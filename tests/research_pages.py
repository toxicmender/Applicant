"""Synthetic public structured-data fixtures shared by capability and flow tests."""

import json


def posting(url):
    return {
        '@context': 'https://schema.org',
        '@type': 'JobPosting',
        'title': 'Senior Python Engineer',
        'identifier': {'value': 'REQ-42'},
        'url': url,
        'hiringOrganization': {'@type': 'Organization', 'name': 'Example Employer'},
        'datePosted': '2026-11-01',
        'jobLocationType': 'TELECOMMUTE',
        'employmentType': 'FULL_TIME',
        'jobLocation': {'address': {'addressLocality': 'Bengaluru', 'addressCountry': 'IN'}},
        'baseSalary': {
            'currency': 'USD',
            'value': {'minValue': 100000, 'maxValue': 120000, 'unitText': 'YEAR'},
        },
    }


def html(record):
    return '<script type="application/ld+json">' + json.dumps(record) + '</script>'
