import unittest

from applicant.boards.weworkremotely import WeWorkRemotely


class WWREligibilityTest(unittest.TestCase):
    def test_worldwide_location(self):
        rss = b'<rss><channel><item><title>Employer: Engineer</title><link>https://weworkremotely.com/remote-jobs/test</link><region>Anywhere in the World</region></item></channel></rss>'
        self.assertEqual(len(WeWorkRemotely.parse(rss, location='India')), 1)
        self.assertEqual(
            len(
                WeWorkRemotely.parse(
                    rss.replace(b'Anywhere in the World', b'United States'), location='India'
                )
            ),
            0,
        )
