from selenium import webdriver
import json

class LinkedIn:
    def __init__(self, path = None):
        self.browser = webdriver.Chrome(path)

    def login(self, username, password, twoFA = False):
        self.browser.get('https://www.linkedin.com/login?fromSignIn=true')
        self.browser.find_element_by_id('username').send_keys(username)
        self.browser.find_element_by_id('password').send_keys(password)
        self.browser.find_element_by_xpath('/html/body/div/main/div/form/div[4]/button').click()
        if twoFA:
            self.browser.find_element_by_id('input__phone_verification_pin').send_keys(input('Enter OTP: '))
            self.browser.find_element_by_id('two-step-submit-button').click()
            browser.find_element_by_xpath('/html/body/div/div[1]/section/div[2]/div/article/footer/div/div/span/button').click()
        with open('cookies.json', 'w') as f:
            for cookie in browser.get_cookies():
                if '.linkedin.com' in cookie['domain']:
                    json.dump(cookie, f)


