from applicant.domain.research import Compensation


def test_preserves_back_weighted_vesting_and_original_currency():
    raw = {
        'currency': 'INR',
        'component': 'equity',
        'measurement': 'benchmark',
        'details': {
            'payment_type': 'equity_grant',
            'statistic': 'percentile',
            'percentile': '75',
            'valuation_date': '2026-11-01',
            'cliff_months': 12,
            'vesting': [
                {'months': 12, 'percent': '5'},
                {'months': 24, 'percent': '15'},
                {'months': 36, 'percent': '40'},
                {'months': 48, 'percent': '40'},
            ],
        },
    }
    value = Compensation.model_validate(raw).model_dump(mode='json')
    assert value['currency'] == 'INR'
    assert value['details'] == raw['details']


def test_absent_details_preserve_legacy_serialization():
    legacy = Compensation(currency='USD').model_dump(mode='json')
    assert 'details' not in legacy
