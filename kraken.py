import json
import logging
import os
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from queue import Queue, Empty
from urllib.parse import urlparse, unquote, urlsplit

import requests
from bs4 import BeautifulSoup, NavigableString
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

STANDARD_LOG_FORMAT = "[%(levelname)s][%(asctime)s]: %(message)s"
STANDARD_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(level=logging.ERROR,
                    format=STANDARD_LOG_FORMAT, datefmt=STANDARD_LOG_DATE_FORMAT,
                    handlers=[logging.FileHandler("kraken.log"),
                              logging.StreamHandler(sys.stdout)
                              ]
                    )

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


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
            },
            {
                "condition_string": "How-To Blended Learning",
                "include_condition": False
            },
            {
                "condition_string": "KÃ¶rpersprache",
                "include_condition": False
            },
            {
                "condition_string": "SS21 Kommunikation &amp; Verhandlungstechnik",
                "include_condition": False
            },
            {
                "condition_string": "Arbeits- und PrÃ¤sentationstechniken",
                "include_condition": False
            },
            {
                "condition_string": "Konfliktmanagement",
                "include_condition": False
            },
            # {
            #     "condition_string": "Parallel Pro",
            #     "include_condition": True
            # }

        ]
        self.FILTER_FILETYPES = []  # ["mat", "csv",...]
        self.THREAD_COUNT = 12
        self.TIMEOUT = 60
        self.DOWNLOAD_PATH = "./scraper_test/"
        self.MAX_FILE_SIZE_IN_MB = 200
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
            logger.error("failed to initialize webdriver for selenium, /"
                         "make sure you downloaded a driver and wrote the correct path to config, /"
                         "current path: " + path)
            logger.error(e)

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
                    logger.error("request unable to get: " + URL)
                return None

        if page.status_code == 303:
            logger.debug("request got redirected: " + URL + " to " + page.url)
            # this means the file is a forced download, so we can't scrape the page
            raise RedirectException(page.headers["Location"])

        soup = BeautifulSoup(page.content, self.Parser)

        if soup is None:
            logger.error("No soup could be cooked for" + URL + " !")

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
                    logger.error("chromeDriver unable to get: " + URL)
                return None

        soup = BeautifulSoup(page, self.Parser)

        if soup is None:
            logger.error("No soup could be cooked for" + URL + " !")

        return soup

    def shutdown(self):
        if self.driver:
            logger.info("shutting down webdriver")
            self.driver.quit()


class Kraken:

    def __init__(self, scraping_config):
        self.config = scraping_config
        self.to_visit = Queue()
        self.to_visit.put({"url": self.config.BASE_URL, "type": "base"})
        self.visited = set()
        self.files = Queue()
        self.domain = urlsplit(self.config.BASE_URL).netloc
        self.pool = ThreadPoolExecutor(max_workers=self.config.THREAD_COUNT)
        self.session = None
        self.soupChef = SoupChef({"MAX_RETRY": 4, "TIMEOUT": 60, "WEBDRIVER_DIR": scraping_config.WEBDRIVER_DIR,
                                  "WEBDRIVER_FILE": scraping_config.WEBDRIVER_FILE})
        self.ajaxCalls = (
            'https://elearning.fhws.de/theme/remui/request_handler.php?action=get_courses_ajax&wdmdata={'
            '%22category%22:%22all%22,%22sort%22:null,%22search%22:%22%22,%22tab%22:true,%22page%22:{%22courses%22:0,'
            '%22mycourses%22:%22 ',
            '%22},%22pagination%22:true,%22view%22:%22grid%22,%22isFilterModified%22:true}')

    def run(self):
        # first login to get the session cookies and then start the scraping with a session for every thread with
        # saved cookies
        self._init_session()
        self._do_login()

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
                logger.error(e)
                break

        logger.info("finished scraping")
        logger.info(f"visited: {len(self.visited)}")

    def scrape(self, target):
        url = target["url"]

        if self._is_relative_URL(url):
            source_URL = self.config.BASE_URL + url
        else:
            source_URL = url

        try:
            if target["type"] == "base":
                courses = self._filter(self._get_courses())
                for course in courses:
                    if course not in self.visited:
                        self.to_visit.put({"url": course, "type": "course"})

            elif target["type"] == "course":
                self.parse_coursepage(source_URL)

            else:
                if target["name"] == "404":
                    print("hi 404")
                file_name, file_url = self.parse_filepage(source_URL)
                if file_name is not None and file_url is not None:
                    if self.domain != urlparse(file_url).netloc:
                        logger.debug(f"skipping {file_url} because it is not on the same domain")
                        return
                    self.save_file({"file_name": file_name, "file_url": file_url, "folder_name": target["block"],
                                    "course_name": target["course"]})

        except Exception as e:
            logger.error(e)
            print(e)

    def _do_login(self):
        flag = load_dotenv(config.CREDENTIALS)
        if not flag:
            raise Exception("Credentials file not found")

        logger.info("successfully loaded credentials-file")

        login_url, token = self._get_form_data(self.config.BASE_URL)  # 'https://elearning.fhws.de/login/index.php'
        login_data = {
            'username': os.environ.get('STUDENT_USER'),
            'password': os.environ.get('STUDENT_PASSWORD'),
            'logintoken': token
        }
        response = self.session.post(login_url, data=login_data)
        if response.status_code != 200:
            logger.error("login failed")
            return
        logger.info("login successful")

    def _get_courses(self):
        index = 0
        courses = []

        response = self.session.get(self.ajaxCalls[0] + str(index) + self.ajaxCalls[1])
        if response.status_code == 200:
            logger.info(f"ajax call (nr: {index}) successful - received {len(response.json()['courses'])} courses")
        else:
            logger.error(f"ajax call (nr: {index}) failed")

        content = response.json()
        requested_courses = content["courses"]
        if len(requested_courses) == 0:
            logger.error("no courses received during ajax call")
            return
        courses.extend(requested_courses)
        index += 1

        if "pagination" in content:
            maxIndex = max(map(int, re.findall("page=(\d)", content["pagination"])))
        else:
            maxIndex = 0

        while index <= maxIndex:
            response = self.session.get(self.ajaxCalls[0] + str(index) + self.ajaxCalls[1])
            if response.status_code == 200:
                logger.info(f"ajax call (nr: {index}) successful - received {len(response.json()['courses'])} courses")
            else:
                logger.error(f"ajax call (nr: {index}) failed")

            requested_courses = response.json()["courses"]
            if len(requested_courses) == 0:
                logger.error("no courses received during ajax call (nr: {index})")
                return

            courses.extend(requested_courses)
            index += 1

        logger.info(f"found {len(courses)} courses")
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

        course_name = soup.select_one("h1").text.strip()

        # is this a special course with tiles?
        is_existing = soup.select_one("div#card-container")
        if is_existing is not None:
            logger.debug(f"found special course {course_name}")
            courses = soup.select("li.section a[href*='course']")
            courses = [course["href"] for course in courses]
            for course in courses:
                if course not in self.visited:
                    self.to_visit.put({"url": course, "type": "course"})
            return

        # find all blocks on the page
        blocks = soup.select("li.section")
        if blocks is None:
            logger.error(f"no blocks found on {source_URL}")
            return

        flag = True

        # iterate over all blocks
        for block in blocks:
            # find name of the block (e.g. "Lernmaterialien")
            block_name = block.select_one("h4 > a")
            if block_name is None:
                block_name = block.select_one("h4 div")
                if block_name is None:
                    #  logger.error(f"no block name found on {source_URL}")
                    continue
            if "&section" in source_URL:
                block_name = block.select_one("h2.section-title")

            block_name = block_name.text.strip()
            # for each block, find all links
            # links = self._filter(block.select("a[href]"))
            links = block.select("a:not([href^='#'])")
            # names = block.select("a[onclick] > span")
            names = []
            for link in links:
                try:
                    if len(link.contents) == 1 and isinstance(link.contents[0], NavigableString):
                        names.append(link.contents[0].strip())
                    else:
                        tag = link.select_one("span:not(.fp-icon)")
                        if tag is not None:
                            names.append(tag.contents[0].strip())
                        else:
                            names.append("404")
                except Exception as e:
                    logger.error(f"error while parsing block {block_name} of course {course_name}: {e}")

            if len(links) == 0:
                #logger.warning(f"no links found in block {block_name} of course {course_name} on {source_URL}")
                continue

            # iterate over all links
            for idx, elem in enumerate(links):
                try:
                    name = names[idx]
                except IndexError as e:
                    name = "404"
                    logger.error(f"no name found for link {elem['href']} in block {block_name} of course {course_name}")
                link = elem["href"]
                # TODO filter redirects
                # TODO make constants
                # TODO beautify
                if "questionnaire" in link or "forum" in link or "groupselect" in link \
                        or "assign" in link or "page" in link or "workshop" in link or "data" in link or "user" in link \
                        or "quiz" in link or "feedback" in link or "choicegroup" in link or "choice" in link \
                        or "evaluation" in link or "scorm" in link or "lesson" in link or "lightboxgallery" in link \
                        or "glossary" in link or "chat" in link or "#" in link or "mailto" in link:
                    continue

                #if "url" in link:
                    #logger.debug(f"{link} is type url")

                if link not in self.visited:
                    self.to_visit.put(
                        {"type": "file", "url": link, "name": name, "block": block_name, "course": course_name})
                    flag = False

            logger.debug(f"found {len(links)} links in block {block_name} of course {course_name}")

        if flag:
            logger.warning(f"no new links found in course {course_name} {source_URL}")

    def parse_filepage(self, source_URL):
        fileName, fileURL = None, None
        try:
            if self.domain != urlparse(source_URL).netloc:
                logger.debug(f"skipping {source_URL} because it is not on the same domain")
                return fileName, fileURL

            pathEnding = os.path.splitext(source_URL)[-1]
            if pathEnding != "" and not pathEnding.startswith(".php"):
                # is file url
                fileName = os.path.basename(urlparse(source_URL).path)
                fileURL = source_URL
                return fileName, fileURL

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
                tag = soup.select_one(".resourceworkaround a[onclick], .urlworkaround a")
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
            logger.error(f"failed to parse filepage {source_URL}: {e}")
            return fileName, fileURL

        logger.debug(f"found file {fileName} at {fileURL}")

        # TODO filter by filetype
        return fileName, fileURL

    def _filter(self, elements):
        filteredElements = []
        for element in elements:
            if isinstance(element, dict):
                name = re.search(">(.*)<", element["coursename"]).group(1)
                element = element["courseurl"]
            else:
                name = element.text.strip()

            valid = True
            for condition in self.config.FILTER_COURSES:
                is_included = bool(re.search(condition["condition_string"], name))

                if condition["include_condition"]:
                    valid = valid and is_included
                else:
                    valid = valid and not is_included

            if valid:
                filteredElements.append(element)

        logger.info(f"filtered {len(elements) - len(filteredElements)} elements")
        return filteredElements

    def _init_session(self):
        self.session = requests.Session()
        if self.config.URLLIB_POOLSIZE:
            logger.info(f"setting maxsize of urllib pool to: {self.config.URLLIB_POOLSIZE}")
        self.session.get_adapter(self.config.BASE_URL).poolmanager.connection_pool_kw[
            "maxsize"] = self.config.URLLIB_POOLSIZE

    def save_file(self, param):
        file_name = param["file_name"].replace(" ", "_")
        file_name = file_name[0:-6].replace(".", "_") + file_name[-6:]
        file_url = param["file_url"]
        block_name = slugify(param["folder_name"])
        course_name = slugify(param["course_name"])

        try:
            # first head to get size
            # TODO do that only for certain file types
            try:
                file_size = int(self.session.head(file_url).headers["Content-Length"])
            except Exception as e:
                logger.error(f"failed to get file size of {file_name} from {course_name}: {e}")
                file_size = 1
            if (file_size / 1000 ** 2) > self.config.MAX_FILE_SIZE_IN_MB:
                logger.info(f"file {file_name} from {course_name} is too big: {file_size / 1000 ** 2} MB")
                return
            # get file bytes
            file = self.session.get(file_url)
            file_bytes = file.content

            file_path = os.path.join(self.config.DOWNLOAD_PATH, course_name, block_name)
            file_type = os.path.splitext(file_name)[-1]
            if file_type == "":
                # TODO set default as a constant
                file_name += ".zip"
                # file_type = ".zip"
            full_path = os.path.join(file_path, file_name)
            os.makedirs(file_path, exist_ok=True)
            with open(full_path, "wb") as f:
                f.write(file_bytes)
        except Exception as e:
            logger.error(f"error while saving file {file_name} from {file_url}: {e}")
        logger.debug(f"saved file {file_name} of {course_name}")

    def _shutdown(self):
        logger.info("shutting down pool and soupChef")
        self.pool.shutdown(wait=True)
        self.soupChef.shutdown()


if __name__ == '__main__':
    config = Config()
    kraken = Kraken(config)
    kraken.run()

# TODO: - stop filtering /url/ links (see blockchain course as they link videos as this)
#       - more or less filter by looking at further link and stay in the domain
#       - see parallele und verteilte systeme -> scorm videos aber abfuck
# PROBLEME:
# - https://elearning.fhws.de/course/view.php?id=18347
# - https://elearning.fhws.de/mod/url/view.php?id=541300
