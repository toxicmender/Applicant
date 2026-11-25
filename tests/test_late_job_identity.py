from applicant.domain.research import ListingObservation
from applicant.domain.research_identity import match_jobs


def observation(source, version, stamp, requisition=None):
    return ListingObservation.model_validate(
        {
            'job_version_id': version,
            'job': {'id': '300', 'title': 'Engineer', 'company': 'Example', 'source': source},
            'status': 'open',
            'origin': 'unknown',
            'acquired_at': stamp,
            'source_url': f'https://{source}.example/jobs/300',
            'content_sha256': 'a' * 64,
            'employer_requisition_url': requisition,
        }
    )


def test_late_identity_evidence_survives_newer_search_observation():
    first = '2026-11-01T00:00:00Z'
    later = '2026-11-02T00:00:00Z'
    final = '2026-11-03T00:00:00Z'
    requisition = 'https://employer.example/jobs/42'
    one = observation('wellfound', 'first', first)
    two = observation('builtin', 'second', first, requisition)
    original = match_jobs([one, two])
    assert all(group['status'] == 'possible_match' for group in original)

    detail = observation('wellfound', 'detail', later, requisition)
    search = observation('wellfound', 'search', final)
    result = match_jobs([search, two, detail, one])
    assert len(result) == 1
    assert result[0]['status'] == 'confirmed'
    assert set(result[0]['job_version_ids']) == {'search', 'second'}
    assert match_jobs([search, two], evidence=[detail, one]) == result
    assert match_jobs([two, one]) == original
