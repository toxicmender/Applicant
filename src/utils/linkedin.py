from selenium import webdriver
from selenium.common import exceptions
from selenium.webdriver.chrome.options import Options
from pathlib import Path
import json
import time

class LinkedIn:
    def __init__(self, path = None, headless = True):
        self.browser_options = Options()
        self.browser_options.add_argument('--disable-extensions')
        if headless:
            self.browser_options.add_argument('--headless')
        self.browser = webdriver.Chrome(path, options=self.browser_options)

    def login(self, username, password, twoFA = False, filepath = 'cookies.json', overwrite = False):
        self.browser.get('https://www.linkedin.com/login?fromSignIn=true')
        self.browser.find_element_by_id('username').send_keys(username)
        self.browser.find_element_by_id('password').send_keys(password)
        # TODO: Detect which one is correct on the page
        # self.browser.find_element_by_xpath('/html/body/div/main/div/form/div[3]/button').click()
        if self.browser.find_element_by_xpath('/html/body/div/main/div/form/div[4]/button'):
            self.browser.find_element_by_xpath('/html/body/div/main/div/form/div[4]/button').click()
        else:
            self.browser.find_element_by_xpath('/html/body/div/main/div/form/div[3]/button').click()

        if twoFA:
            self.browser.find_element_by_id('input__phone_verification_pin').send_keys(input('Enter OTP: '))
            self.browser.find_element_by_id('two-step-submit-button').click()
            # TODO: Check if it opens everytime or just the first time on a new OS/IP Address pair
            # self.browser.find_element_by_xpath('/html/body/div/div[1]/section/div[2]/div/article/footer/div/div/span/button').click()

        cookies = {'list': []}
        for cookie in self.browser.get_cookies():
            if '.linkedin.com' in cookie['domain']:
                cookies['list'].append(cookie)

        if overwrite:
            with open(filepath, 'w') as file:
                json.dump(cookies, file)
        else:
            try:
                with open(filepath, 'x') as file:
                    json.dump(cookies, file)

            except FileExistsError as error:
                print(error)
                print('File specified already exists. Please use overwrite flag if you wish to login again or use `restore_session(filepath)` method instead to load the saved cookies')

    def restore_session(self, filepath = 'cookies.json'):
        self.browser.get('https://www.linkedin.com/')
        try:
            with open(filepath, 'r') as file:
                cookies = json.load(file)
                for cookie in cookies['list']:
                    if 'expiry' in cookie.keys():
                        del cookie['expiry']
                    self.browser.add_cookie(cookie)
            print('Cookies loaded into browser successfully')
            # Reload page with the cookies
            self.browser.get('https://www.linkedin.com/feed')

        except FileNotFoundError as error:
            print(error)
            print('Failed to add Cookies to the browser')

    def scrape_jobs(self, filepath = 'job_listing.json'):
        # Get to the page
        self.browser.get('https://www.linkedin.com/jobs')
        # Get scroll height
        last_height = self.browser.execute_script("return document.body.scrollHeight")

        # Get the initially loaded jobs
        jobs = {'list': [{{s.split('=')[0]: s.split('=')[-1] for s in job.get_attribute('href').split('&')}['currentJobId']: job.get_attribute('href')}  for job in self.browser.find_elements_by_class_name('job-card__link-wrapper')]}

        # Start scrolling upto maximum allowed pages to lost as many jobs as possible
        while True:
            # Scroll down to bottom
            self.browser.execute_script("window.scrollTo(0, document.body.scrollHeight);")

            # Wait to load page, time is tentative but depends on network connection speed + lazy loaded component rendering
            time.sleep(3)

            # Calculate new scroll height and compare with last scroll height
            new_height = self.browser.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height

        # Get list of newly loaded job cards
        for job in self.browser.find_elements_by_class_name('job-card__link-wrapper'):
            url = job.get_attribute('href')
            jobID = {s.split('=')[0]: s.split('=')[-1] for s in url.split('&')}['currentJobId']
            try:
                job.find_element_by_class_name('job-card__easy-apply')
                jobs['list'].append({jobID: url})
                # So that loop ignores the entries without Easy Apply and continues the loop without interruption
            except exceptions.NoSuchElementException as error:
                print(error)

        if Path(filepath).exists():
            with open(filepath, 'r') as file:
                # payload = json.load(file)
                for job in json.load(file)['list']:
                    jobs['list'].append(job)

            with open(filepath, 'w') as file:
                json.dump(jobs, file)
        else:
            print('creating a new file')
            with open(filepath, 'x') as file:
                json.dump(jobs, file)

    def easy_apply(self, filepath = 'job_listing.json'):
        try:
            with open(filepath, 'r') as file:
                jobs = json.load(file)
                for jobID in jobs.keys():
                    # TODO: prevent openning & applying on already applied job postings
                    self.browser.get(jobs[jobID])
                    self.browser.find_element_by_class_name('jobs-apply-button').click()
                    if self.browser.find_element_by_id('follow-company-checkbox').is_selected():
                        # self.browser.find_element_by_id('follow-company-checkbox').click() results in selenium.common.exceptions.ElementClickInterceptedException
                        self.browser.execute_script("arguments[0].click();", self.browser.find_element_by_id('follow-company-checkbox'))
                    self.browser.find_element_by_class_name('artdeco-button--primary').click()
        except FileNotFoundError as error:
            print(error)

    def __del__(self):
        self.browser.quit()
