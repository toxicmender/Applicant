#!/usr/bin/env python

import argparse
import getpass
from pathlib import Path
from utils.linkedin import LinkedIn

parser = argparse.ArgumentParser(description='Automatically apply for recommened/filtered jobs for your LinkedIn Profile')

parser.add_argument('-c', '--cookies', default='cookies.json', help='file path to where cookies are or to store them')
parser.add_argument("-w", "--overwrite", action="store_true", help='overwrite existing session if it already exists')
parser.add_argument('-d', '--driver', default='chromedriver', help='path to Chrome browser driver')
parser.add_argument("-t", "--twofa", action="store_true", help='use it if you have 2 Factor Authentication enabled')
parser.add_argument('-j', '--jobs', default='job_listing.json', help='file path to where jobs urls are or to store them')
parser.add_argument("-D", "--Display", action="store_false", help='Whether to display the browser or not (headless mode)')

args = parser.parse_args()

# Driver Code

operator = LinkedIn(path = args.driver, headless = args.Display)

fp = Path(args.cookies)

if args.overwrite:
    user = input('Username/Email ID: ')
    passw = getpass.getpass()
    operator.login(username=user, password=passw, twoFA=args.twofa, filepath=fp, overwrite=args.overwrite)
elif fp.exists():
    operator.restore_session(fp)
else:
    user = input('Username/Email ID: ')
    passw = getpass.getpass()
    operator.login(username=user, password=passw, twoFA=args.twofa, filepath=fp, overwrite=args.overwrite)

operator.scrape_jobs(args.jobs)
# Uncooment for LinkedIn's easy apply
# operator.easy_apply(args.jobs)
