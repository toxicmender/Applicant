# Applicant
It is a dynamic web scraper which gets the posted jobs which are applicable from within the site and don't redirect to another site. It then applies to them automatically with your stored (Latest) Resume.
> _**Note: Filter operation hasn't been implemented yet for jobs**_

## Usage
1. Download Google's [Chromium Drivers](https://sites.google.com/a/chromium.org/chromedriver/downloads) & either add to Path or put it in the `src/` directory
2. `pip install -r requirements.txt`
3. Run `python run.py -h` or `python run.py --help` to see the full list of arguments supported
4. `python run.py` without arguments it'll create 2 files in current directory by the name of `cookies.json` storing session cookies & `job_listing.json` for scraped jobs.
