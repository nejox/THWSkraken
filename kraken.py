import json
import logging
import os
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
import time
from queue import Queue, Empty
from urllib.parse import urlparse, unquote
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

STANDARD_LOG_FORMAT = "[%(levelname)s][%(asctime)s]: %(message)s"
STANDARD_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(level=logging.INFO,
                    format=STANDARD_LOG_FORMAT, datefmt=STANDARD_LOG_DATE_FORMAT,
                    handlers=[logging.FileHandler("kraken.log"),
                              logging.StreamHandler(sys.stdout)
                              ]
                    )


def slugify(value, allow_unicode=True):
    """
    Taken from https://github.com/django/django/blob/master/django/utils/text.py
    Convert to ASCII if 'allow_unicode' is False. Convert spaces or repeated
    dashes to single dashes. Remove characters that aren't alphanumerics,
    underscores, or hyphens. Convert to lowercase. Also strip leading and
    trailing whitespace, dashes, and underscores.
    """
    value = str(value)
    value.replace("ä", "ae").replace("Ä", "Ae").replace("ö", "oe").replace("Ö", "Oe").replace("ü", "ue").replace("Ü",
                                                                                                                 "Ue").replace(
        "ß", "ss")
    if allow_unicode:
        value = unicodedata.normalize('NFKC', value)
    else:
        value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
    value = re.sub(r'[^\w\s-]', '', value.lower())
    return re.sub(r'[-\s]+', '_', value).strip('-_')


class Config:
    def __init__(self, config_file=""):
        self.BASE_URL = "https://elearning.fhws.de/course/index.php?mycourses=1"
        self.element_selector = ""
        self.FILTER_COURSES = [
            {
                "condition_string": "Gründen@FHWS",
                "include_condition": False
            },
            {
                "condition_string": "Robotics",
                "include_condition": False
            },
            {
               "condition_string": "Counselling",
               "include_condition": False
            },
            {
                "condition_string": "CampusPortal",
                "include_condition": False
            },
            {
                "condition_string": "General information for the Master Artificial Intelligence",
                "include_condition": False
            },
            {
                "condition_string": "FIW Internationales",
                "include_condition": False
            }
        ]
        self.FILTER_FILETYPES = []  # ["mat", "csv",...]
        self.THREAD_COUNT = 12
        self.TIMEOUT = 60
        self.DOWNLOAD_PATH = "./scraper_test/"

        self.WEBDRIVER_DIR = "./drivers"
        self.WEBDRIVER_FILE = "chromedriver.exe"
        self.CREDENTIALS = "credentials.env"
        self.URLLIB_POOLSIZE = 15

        if config_file != "":
            self.read_config(config_file)

    def read_config(self, config_file):
        with open(config_file, 'r') as f:
            cf_json = json.load(f)
            self.BASE_URL = cf_json.get("baseURL", self.BASE_URL)
            self.element_selector = cf_json.get("element_selector", self.element_selector)
            self.FILTER_COURSES = cf_json.get("filter_courses", self.FILTER_COURSES)
            self.DOWNLOAD_PATH = cf_json.get("saveDirectory", self.DOWNLOAD_PATH)

            self.THREAD_COUNT = cf_json.get("threadCount", self.THREAD_COUNT)
            self.TIMEOUT = cf_json.get("timeout", self.TIMEOUT)
            self.WEBDRIVER_DIR = cf_json.get("webdriver_dir", self.WEBDRIVER_DIR)
            self.WEBDRIVER_FILE = cf_json.get("webdriver_file", self.WEBDRIVER_FILE)
            self.CREDENTIALS = cf_json.get("credentials", self.CREDENTIALS)


class RedirectException(Exception):
    """Raised when the file url redirects to the download"""
    def __init__(self, new_url):
        self.new_url = new_url


class SoupChef:

    def __init__(self, driverConfig=None):
        if driverConfig is None:
            self.config = {
                "MAX_RETRY": 3,
                "TIMEOUT": 60,
                "WEBDRIVER_DIR": "./drivers",
                "WEBDRIVER_FILE": "chromedriver.exe"
            }
        else:
            self.config = driverConfig

        self.driver = None
        self.soup = None
        self.Parser = 'html.parser'

    def _get_webdriver(self):
        """
        returns a webdriver for selenium
        expects you to have the file in a directory named after your os (linux / windows / if you use mac, go buy linux)
        https://www.makeuseof.com/how-to-install-selenium-webdriver-on-any-computer-with-python/
        """
        path = None
        try:
            driver_options = Options()
            # driver_options.add_experimental_option('excludeSwitches', ['enable-logging'])
            # trying to block logging from webdriver as it spams the log unnecessarily
            driver_options.headless = True

            if os.name == 'posix':
                path = os.path.join(self.config["WEBDRIVER_DIR"], "linux", self.config["WEBDRIVER_FILE"])
                ser = Service(path)
                return webdriver.Chrome(service=ser, options=driver_options, service_log_path='/dev/null')
            else:
                path = os.path.join(self.config["WEBDRIVER_DIR"], "windows", self.config["WEBDRIVER_FILE"])
                ser = Service(path)
                return webdriver.Chrome(service=ser, options=driver_options, service_log_path='NUL')

        except Exception as e:
            logging.error("failed to initialize webdriver for selenium, /"
                          "make sure you downloaded a driver and wrote the correct path to config, /"
                          "current path: " + path)
            logging.error(e)

    def get_soup_from_URL(self, URL, session=None, dynamic=False):
        if dynamic:
            if not self.driver:
                self.driver = self._get_webdriver()
            self.soup = self._get_soup_of_dynamic_page(URL, session=session)
        else:
            self.soup = self._get_soup_of_static_page(URL, session=session)
        return self.soup

    def get_soup_from_text(self, text):
        self.soup = BeautifulSoup(text, self.Parser)
        return self.soup

    def _get_soup_of_static_page(self, URL, session=None):
        """
        parses the given URL and returns the soup
        object of a static loaded page
        :param URL: the URL to parse
        :return: the soup object
        """
        page = None
        retry_count = 0
        while page is None and retry_count < self.config["MAX_RETRY"]:
            try:
                retry_count += 1
                if session is None:
                    page = requests.get(URL, timeout=self.config["TIMEOUT"], allow_redirects=False)
                else:
                    page = session.get(URL, timeout=self.config["TIMEOUT"], allow_redirects=False)

            except Exception as e:
                if retry_count == self.config["MAX_RETRY"] - 1:  # TODO: make this a config option
                    logging.error("request unable to get: " + URL)
                    return None

        if page.status_code == 303:
            logging.info("request got redirected: " + URL + " to " + page.url)
            # this means the file is a forced download, so we can't scrape the page
            raise RedirectException(page.headers["Location"])

        soup = BeautifulSoup(page.content, self.Parser)

        if soup is None:
            logging.error("No soup could be cooked for" + URL + " !")

        return soup

    def _get_soup_of_dynamic_page(self, URL, session=None):
        """
        parses the given URL and returns the soup
        object of a dynamic loaded page
        :param URL: the URL to parse
        :return: the soup object
        """
        if session is not None:
            for cookie in session.cookies:
                self.driver.add_cookie({'name': cookie.name, 'value': cookie.value})

        page = None
        retry_count = 0
        while page is None and retry_count < self.config["MAX_RETRY"]:
            try:
                retry_count += 1
                self.driver.get(URL)
                # time.sleep(1)  # load page
                page = self.driver.page_source

            except Exception as e:
                if retry_count == self.config["MAX_RETRY"] - 1:
                    logging.error("chromeDriver unable to get: " + URL)
                    return None

        soup = BeautifulSoup(page, self.Parser)

        if soup is None:
            logging.error("No soup could be cooked for" + URL + " !")

        return soup

    def shutdown(self):
        if self.driver:
            logging.info("shutting down webdriver")
            self.driver.quit()


class Kraken:

    def __init__(self, scraping_config):
        self.config = scraping_config
        self.to_visit = Queue()
        self.to_visit.put({"url": self.config.BASE_URL, "type": "base"})
        self.visited = set()
        self.files = Queue()
        self.pool = ThreadPoolExecutor(max_workers=self.config.THREAD_COUNT)
        self.session = None
        self.soupChef = SoupChef({"MAX_RETRY": 4, "TIMEOUT": 60, "WEBDRIVER_DIR": scraping_config.WEBDRIVER_DIR,
                                  "WEBDRIVER_FILE": scraping_config.WEBDRIVER_FILE})
        self.ajaxCalls = (
            'https://elearning.fhws.de/theme/remui/request_handler.php?action=get_courses_ajax&wdmdata={'
            '"category":"all","sort":null,"search":"","tab":true,"page":{"courses":0,"mycourses":',
            '},"pagination":true,"view":null,"isFilterModified":true}')

    def run(self):
        # first login to get the session cookies and then start the scraping with a session for every thread with
        # saved cookies
        self._init_session()
        self._do_login()
        # self._init_soupChef()
        while True:
            try:
                target = self.to_visit.get(block=True, timeout=15)  # first time takes some while..
                self.visited.add(target["url"])
                self.pool.submit(self.scrape, target)
                #self.scrape(target)

            except Empty:
                self._shutdown()
                break
            except Exception as e:
                print(e)
                logging.error(e)
                break

        logging.info("finished scraping")
        print("visited: ", len(self.visited))

    def scrape(self, target):
        url = target["url"]

        if self._is_relative_URL(url):
            source_URL = self.config.BASE_URL + url
        else:
            source_URL = url

        if target["type"] == "base":
            courses = self._filter(self._get_courses())
            for course in courses:
                if course not in self.visited:
                    self.to_visit.put({"url": course, "type": "course"})

        elif target["type"] == "course":
            self.parse_coursepage(source_URL)

        else:
            file_name, file_url = self.parse_filepage(source_URL)
            if file_name is not None and file_url is not None:
                self.save_file({"file_name": file_name, "file_url": file_url, "folder_name": target["block"],
                                "course_name": target["course"]})

    def _do_login(self):
        try:
            load_dotenv(config.CREDENTIALS)
        except Exception as e:
            logging.error("could not load credentials-file")
            return
        logging.info("successfully loaded credentials-file")

        login_url, token = self._get_form_data(self.config.BASE_URL)  # 'https://elearning.fhws.de/login/index.php'
        login_data = {
            'username': os.environ.get('STUDENT_USER'),
            'password': os.environ.get('STUDENT_PASSWORD'),
            'logintoken': token
        }
        response = self.session.post(login_url, data=login_data)
        if response.status_code != 200:
            logging.error("login failed")
            return
        logging.info("login successful")

    def _get_courses(self):
        index = 0
        courses = []

        response = self.session.get(self.ajaxCalls[0] + str(index) + self.ajaxCalls[1])
        if response.status_code == 200:
            logging.info(f"ajax call (nr: {index}) successful")
        else:
            logging.error(f"ajax call (nr: {index}) failed")

        content = response.json()
        courses.extend(content["courses"])
        index += 1

        if "pagination" in content:
            maxIndex = max(map(int, re.findall("page=(\d)", content["pagination"])))
        else:
            maxIndex = 0

        while index <= maxIndex:
            response = self.session.get(self.ajaxCalls[0] + str(index) + self.ajaxCalls[1])
            if response.status_code == 200:
                logging.info(f"ajax call (nr: {index}) successful")

                content = response.json()
                courses.extend(content["courses"])
                index += 1

            else:
                logging.error(f"ajax call (nr: {index}) failed")

        logging.info(f"found {len(courses)} courses")
        return courses

    def _get_form_data(self, url):
        response = self.session.get(url)
        soup = self.soupChef.get_soup_from_text(response.text)
        login_url = soup.find('form', {'id': 'login'})['action']
        token = soup.find('input', {'name': 'logintoken'})['value']
        return login_url, token

    @staticmethod
    def _is_relative_URL(URL):
        """
        checks if the given URL starts with http, to determine if it is a relative URL
        lots of webpages return only the path url on their own website
        :param URL: the URL to check
        :return: false if URL starts with http, otherwise true
        """
        return not bool(re.search("^http", URL))

    def parse_coursepage(self, source_URL):

        soup = self.soupChef.get_soup_from_URL(source_URL, self.session)
        if soup is None:
            return

        # find all blocks on the page
        blocks = soup.select(".card.section")
        course_name = soup.select_one("h1").text.strip()
        # iterate over all blocks
        for block in blocks:
            # find name of the block (e.g. "Lernmaterialien")
            block_name = block.select("h4 > a")[0].text.strip()
            # for each block, find all links
            links = block.select("a[onclick]")
            names = block.select("a[onclick] > span")
            # iterate over all links
            for idx, elem in enumerate(links):
                name = names[idx].contents[0].strip()
                link = elem["href"]
                # TODO filter redirects
                # TODO make constants
                # TODO beautify
                if "url" in link or "questionnaire" in link or "forum" in link or "groupselect" in link \
                        or "assign" in link or "page" in link or "workshop" in link or "data" in link:
                    continue
                if link not in self.visited:
                    self.to_visit.put(
                        {"type": "file", "url": link, "name": name, "block": block_name, "course": course_name})
            logging.info(f"found {len(links)} links in block {block_name} of course {course_name}")

    def parse_filepage(self, source_URL):
        fileName, fileURL = None, None
        try:

            soup = self.soupChef.get_soup_from_URL(source_URL, self.session)
            if soup is None:
                return fileName, fileURL

            is_folder = bool(re.search("folder", source_URL))

            if is_folder:
                # file name
                fileName = soup.select_one("h2").text.strip().replace("/", "_")
                # find form data
                form_tag = soup.select_one("form:not([id])[method=post]")
                method = form_tag["method"]
                action = form_tag["action"]
                value = soup.select_one("input[name=id]")["value"]
                fileURL = action + "?id=" + value

            else:
                # find data link
                tag = soup.select_one(".resourceworkaround a[onclick]")
                if tag is None:
                    # TODO beautify
                    # may be a embedded pdf file like in "https://elearning.fhws.de/mod/resource/view.php?id=681498"
                    # could also be embedded image like in https://elearning.fhws.de/mod/resource/view.php?id=686598
                    tag = soup.select_one("object a")
                if tag is None:
                    tag = soup.select_one("img.resourceimage")
                    fileURL = tag["src"]
                    fileName = soup.select_one("h2").text.strip()
                else:
                    fileURL = tag["href"]
                    fileName = tag.text


        except RedirectException as e:
            # some resource urls automatically redirect to the file
            fileURL = e.new_url
            fileName = unquote(urlparse(e.new_url).path.split("/")[-1])
            return fileName, fileURL

        except Exception as e:
            logging.error(f"failed to parse filepage {source_URL}")
            return fileName, fileURL

        logging.info(f"found file {fileName} at {fileURL}")
        # TODO filter by filetype
        return fileName, fileURL

    def _filter(self, elements):

        filteredElements = []
        for element in elements:
            if isinstance(element, dict):
                name = re.search(">(.*)<", element["coursename"]).group(1)
                element = element["courseurl"]
            else:
                link = element.find("a")
                name = element.find("div.text_to_html").text

            valid = True
            for condition in self.config.FILTER_COURSES:
                is_included = bool(re.search(condition["condition_string"], name))

                if condition["include_condition"]:
                    valid = valid and is_included
                else:
                    valid = valid and not is_included

            if valid:
                filteredElements.append(element)

        logging.info(f"filtered {len(elements) - len(filteredElements)} elements")
        return filteredElements

    def _init_session(self):
        self.session = requests.Session()
        if self.config.URLLIB_POOLSIZE:
            logging.info(f"setting maxsize of urllib pool to {self.config.URLLIB_POOLSIZE}")
            self.session.get_adapter(self.config.BASE_URL).poolmanager.connection_pool_kw[
                "maxsize"] = self.config.URLLIB_POOLSIZE

    def save_file(self, param):
        file_name = param["file_name"].replace(" ", "_")
        file_url = param["file_url"]
        block_name = slugify(param["folder_name"])
        course_name = slugify(param["course_name"])

        try:
            file_path = os.path.join(self.config.DOWNLOAD_PATH, course_name, block_name)
            file_bytes = self.session.get(file_url).content
            file_type = os.path.splitext(file_name)[-1]
            if file_type == "":
                # TODO set default as a constant
                file_name += ".zip"
            full_path = os.path.join(file_path, file_name)
            os.makedirs(file_path, exist_ok=True)
            with open(full_path, "wb") as f:
                f.write(file_bytes)
        except Exception as e:
            logging.error(f"error while saving file {file_name} from {file_url}: {e}")
        logging.info(f"saved file {file_name} of {course_name}")


    def _shutdown(self):
        logging.info("shutting down pool and soupChef")
        self.pool.shutdown(wait=True)
        self.soupChef.shutdown()


if __name__ == '__main__':
    config = Config()
    kraken = Kraken(config)
    kraken.run()


# TODO: - stop filtering /url/ links (see blockchain course as they link videos as this)
#       - more or less filter by looking at further link and stay in the domain
#       - see parallele und verteilte systeme -> scorm videos aber abfuck