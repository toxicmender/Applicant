#!/usr/bin/env python

import argparse
import getpass
import os
import utils.linkedin as li

parser = argparse.ArgumentParser(description='Automatically apply for recommened/filtered jobs for your LinkedIn Profile')

parser.add_argument('-f', '--file', action='store_const', default='cookies.json', type=str, help='file path to where cookies are or to store them')
parser.add_argument("-w", "--overwrite", action="store_true", help='overwrite existing session if it already exists')
parser.add_argument('-d', '--driver', action='store_const', default='chromedriver', type=str, help='path to Chrome browser driver')
parser.add_argument("-t", "--twofa", action="store_true", help='use it if you have 2 Factor Authentication enabled')

args = parser.parse_args()

# Driver Code

operator = li.LinkedIn(path = args.driver)

fp = os.path(args.file)

if args.overwrite:
    user = input('Username/Email ID: ')
    passw = getpass.getpass()
    operator.login(username=user, password=passw, twoFA=args.twofa, filepath=fp, overwrite=args.overwrite)
elif fp.exists():
    operator.restore_session(args.file)
else:
    user = input('Username/Email ID: ')
    passw = getpass.getpass()
    operator.login(username=user, password=passw, twoFA=args.twofa, filepath=fp, overwrite=args.overwrite)
