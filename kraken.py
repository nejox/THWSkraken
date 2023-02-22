import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from queue import Queue, Empty

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service


class Config:
    def __init__(self, config_file=""):
        self.baseURL = "https://elearning.fhws.de/course/index.php?mycourses=1"
        self.element_selector = "h4 a"
        self.filter_courses = [
            {
                "condition_string": "//p/",
                "include_condition": False
            }
        ]
        self.filter_filetypes = []
        self.threadCount = 12
        self.timeout = 60
        self.save_directory = "C:/Users/Jochen/Desktop/testScraper/"

        self.WEBDRIVER_DIR = "./drivers"
        self.WEBDRIVER_FILE = "chromedriver.exe"

        if config_file != "":
            self.read_config(config_file)

    def read_config(self, config_file):
        with open(config_file, 'r') as f:
            cf_json = json.load(f)
            self.baseURL = cf_json.get("baseURL", self.baseURL)
            self.element_selector = cf_json.get("element_selector", self.element_selector)
            self.filter_courses = cf_json.get("filter_courses", self.filter_courses)
            self.save_directory = cf_json.get("saveDirectory", self.save_directory)

            self.threadCount = cf_json.get("threadCount", self.threadCount)
            self.timeout = cf_json.get("timeout", self.timeout)


class Kraken:

    def __init__(self, config):
        self.config = config
        self.queue = Queue()
        self.queue.put(self.config.baseURL)
        self.visited = set()
        self.files = []
        self.pool = ThreadPoolExecutor(max_workers=self.config.threadCount)
        self.driver = self._get_webdriver()

    def run(self):
        # 1st step: find all courses and save all data urls to files[]
        while True:
            try:
                target = self.queue.get(block=True, timeout=4)
                self.visited.add(target)
                # self.pool.submit(self.scrape, target)
                self.scrape(target)
                self.driver.quit()
            except Empty:
                break
            except Exception as e:
                print(e)

        # 2nd step: download all files in files[] to directory
        print("visited: ")
        print(self.visited)
        print("------------------")
        print("files: ")
        print(self.files)

    def scrape(self, url):

        if self._is_relative_URL(url):
            source_URL = self.config.baseURL + url
        else:
            source_URL = url

        courses = self.parse_page(source_URL)

    def _get_soup_of_static_page(self, URL):
        """
        parses the given URL and returns the soup
        object of a static loaded page
        :param URL: the URL to parse
        :return: the soup object
        """
        page = None
        retry_count = 0
        while page is None and retry_count < 4:
            try:
                retry_count += 1
                page = requests.get(URL, timeout=self.config.timeout)
            except Exception as e:
                if retry_count == 3:  # TODO: make this a config option
                    logging.error("request unable to get: " + URL)
                    return None

        soup = BeautifulSoup(page.content, 'html.parser')

        if soup is None:
            logging.error("No soup could be cooked for" + URL + " !")

        return soup

    def _get_soup_of_dynamic_page(self, URL):
        """
        parses the given URL and returns the soup
        object of a dynamic loaded page
        :param URL: the URL to parse
        :return: the soup object
        """
        page = None
        retry_count = 0
        while page is None and retry_count < 4:
            try:
                retry_count += 1
                self.driver.get(URL)
                #time.sleep(1)  # load page
                page = self.driver.page_source

            except Exception as e:
                if retry_count == 3:  # TODO: make this a config option
                    logging.error("chromeDriver unable to get: " + URL)
                    return None

        soup = BeautifulSoup(page, 'html.parser')

        if soup is None:
            logging.error("No soup could be cooked for" + URL + " !")

        return soup

    @staticmethod
    def _is_relative_URL(URL):
        """
        checks if the given URL starts with http, to determine if it is a relative URL
        lots of webpages return only the path url on their own website
        :param URL: the URL to check
        :return: false if URL starts with http, otherwise true
        """
        return not bool(re.search("^http", URL))

    def parse_page(self, source_URL):

        soup = self._get_soup_of_dynamic_page(source_URL)
        if soup is None:
            return

        # find all links in the page
        elements = self._filter(soup.find_all(self.config.element_selector))

        # iterate over all links and check if they are a course
        for element in elements:
            if element.has_attr('href'):
                href = element['href']
            else:
                href = element.find("a")['href']

            if self._is_relative_URL(href):
                href = self.config.baseURL + href

            if self._is_course(href):
                self.queue.put(href)
            elif self._is_file(href):
                self.files.append(href)

    def _filter(self, elements):

        filteredElements = []
        for element in elements:
            link = element.find("a")
            name = element.find("div.text_to_html").text

            valid = True
            for condition in self.config.filter_courses:
                is_included = bool(re.search(condition["condition_string"], name))

                if condition["include_condition"]:
                    valid = valid and is_included
                else:
                    valid = valid and not is_included

            if valid:
                filteredElements.append(element)

        return filteredElements

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
                path = os.path.join(config.WEBDRIVER_DIR, "linux", config.WEBDRIVER_FILE)
                ser = Service(path)
                return webdriver.Chrome(service=ser, options=driver_options, service_log_path='/dev/null')
            else:
                path = os.path.join(config.WEBDRIVER_DIR, "windows", config.WEBDRIVER_FILE)
                ser = Service(path)
                return webdriver.Chrome(service=ser, options=driver_options, service_log_path='NUL')

        except Exception as e:
            logging.error("failed to initialize webdriver for selenium, /"
                          "make sure you downloaded a driver and wrote the correct path to config, /"
                          "current path: " + path)
            logging.error(e)


if __name__ == '__main__':
    config = Config()
    kraken = Kraken(config)
    kraken.run()
