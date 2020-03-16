from selenium import webdriver
import json

class LinkedIn:
    def __init__(self, path = None):
        self.browser = webdriver.Chrome(path)

    def login(self, username, password, twoFA = False, filepath = 'cookies.json', overwrite = False):
        self.browser.get('https://www.linkedin.com/login?fromSignIn=true')
        self.browser.find_element_by_id('username').send_keys(username)
        self.browser.find_element_by_id('password').send_keys(password)
        # TODO: Detect which one is correct on the page
        # self.browser.find_element_by_xpath('/html/body/div/main/div/form/div[3]/button').click()
        self.browser.find_element_by_xpath('/html/body/div/main/div/form/div[4]/button').click()

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

            except FileExistsError as err:
                print(err)
                print('session cookie already stored in specified filename')

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
            self.browser.get('https://www.linkedin.com/jobs')

        except FileNotFoundError as err:
            print(err)
            print('Cookies not added to the browser')

    def __del__(self):
        self.browser.quit()
