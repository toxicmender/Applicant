import multiprocessing
from unittest import mock

import httpx
import pytest

from applicant.domain.company_evidence import CompanyEvidenceImport, CompanyResponse
from applicant.infra.store.job_versions import JobVersionRepository
from applicant.infra.store.ranking_common import write_transaction
from applicant.infra.store.sqlite import Store
from applicant.reviews import AmbitionBoxClient, GlassdoorClient
from applicant.services.company_evidence import (
    capture_company_evidence,
    import_company_evidence,
    show_company_evidence,
)
from applicant.settings import Settings
from tests.provider_company_worker import (
    glassdoor_fixture_body,
    glassdoor_fixture_worker,
    hold_lock,
)
from tests.ranking_fixtures import NOW
from tests.test_company_evidence import LOCATORS, association
from tests.test_ranking_engine import version
from tests.test_reviews import PAGE_PROPS, next_data_html


@pytest.mark.parametrize('region', ['com', 'co.in'])
def test_glassdoor_managed_profile_can_capture_anonymous_regional_evidence(tmp_path, region):
    from hashlib import sha256

    from applicant.infra.owned_worker import run_worker
    from applicant.services.provider_setup import provider_session

    config = Settings(data_dir=tmp_path, session_storage='plaintext')
    session = provider_session(config, 'glassdoor')
    credentials = {
        'cookies': [
            {'name': 'account', 'value': 'synthetic', 'domain': '.glassdoor.' + region, 'path': '/'}
        ],
        'origins': [],
    }
    session.install({}, credentials, 'verified', metadata={'region': region})
    job = version('company')
    db = tmp_path / 'company.db'
    locator = LOCATORS['glassdoor'].replace('glassdoor.com', 'glassdoor.' + region)
    associated = association(job, locator=locator)
    with Store(db) as store, write_transaction(store.db):
        JobVersionRepository(store.db).put(job)
    import_company_evidence(db, CompanyEvidenceImport(associations=(associated,)))

    def run(_target, args, **kwargs):
        return run_worker(glassdoor_fixture_worker, (*args, 200), **kwargs)

    with mock.patch('applicant.infra.company_worker.run_worker', side_effect=run):
        report = capture_company_evidence(db, ('glassdoor',), settings=config, clock=lambda: NOW)
        assert report['success'], report
        snapshot_id = report['outcomes'][0]['snapshot']['snapshot_id']
        exported = show_company_evidence(db, snapshot_ids=(snapshot_id,))
        for response in exported['snapshots'][0]['responses']:
            raw = CompanyResponse.model_validate(response)
            assert raw.source_url == locator
            assert raw.body == glassdoor_fixture_body(False).encode()
            assert raw.body_sha256 == sha256(raw.body).hexdigest()
        client = GlassdoorClient(profile_dir=str(session.path.parent))
        client.session_store = session
        client.owned = True
        assert client.fetch(locator, 0).company_id == '42'
        if region == 'com':
            from applicant.errors import ProfileBusy

            ctx = multiprocessing.get_context('spawn')
            ready, release = ctx.Event(), ctx.Event()
            holder = ctx.Process(
                target=hold_lock, args=(session.path.with_suffix('.lock'), ready, release)
            )
            holder.start()
            try:
                assert ready.wait(20)
                with pytest.raises(ProfileBusy):
                    client.fetch(locator, 0)
            finally:
                release.set()
                holder.join(20)
                if holder.is_alive():
                    holder.terminate()
                    holder.join()
                assert holder.exitcode == 0
                holder.close()
        rotated = session.active()
        assert rotated is not None
        assert rotated['cookies'][0]['value'] == 'rotated'
        other = LOCATORS['glassdoor'].replace(
            'glassdoor.com', 'glassdoor.' + ('com' if region == 'co.in' else 'co.in')
        )
        from applicant.errors import ConfigError

        with pytest.raises(ConfigError, match='--reauth --region'):
            client.fetch(other, 0)

    with Store.open_readonly(db) as store:
        assert store.db.execute('SELECT count(*) FROM company_evidence_snapshots').fetchone() == (
            1,
        )


@pytest.mark.parametrize(
    'host, employer', [('glassdoor.co.in.evil.test', '42'), ('glassdoor.co.in', '43')]
)
def test_glassdoor_evidence_rejects_unverified_locator(host, employer):
    with pytest.raises(ValueError, match='exact provider locator'):
        association(locator=f'https://www.{host}/Reviews/Acme-Reviews-E{employer}.htm')


@pytest.mark.parametrize('public_fails', [False, True])
def test_ambitionbox_evidence_export_never_uses_a_managed_account(tmp_path, public_fails):
    from hashlib import sha256

    from applicant.services.provider_setup import provider_session

    config = Settings(data_dir=tmp_path, session_storage='plaintext')
    session = provider_session(config, 'ambitionbox')
    session.install(
        {},
        {
            'cookies': [
                {'name': 'account', 'value': 'synthetic', 'domain': '.ambitionbox.com', 'path': '/'}
            ],
            'origins': [],
        },
        'verified',
    )
    job = version('company')
    db = tmp_path / 'company.db'
    associated = association(job, provider='ambitionbox', locator='tcs', provider_company_id='4')
    with Store(db) as store, write_transaction(store.db):
        JobVersionRepository(store.db).put(job)
    import_company_evidence(db, CompanyEvidenceImport(associations=(associated,)))
    requests = []
    public = next_data_html(PAGE_PROPS).encode()

    def response(request):
        cookie = request.headers.get('cookie')
        requests.append(cookie)
        if cookie:
            return httpx.Response(
                200, content=next_data_html(PAGE_PROPS | {'account': 'PRIVATE_CANARY'}).encode()
            )
        return httpx.Response(404 if public_fails else 200, content=public)

    with httpx.Client(transport=httpx.MockTransport(response)) as http, mock.patch(
        'applicant.infra.company_sources.AmbitionBoxClient',
        side_effect=lambda **options: AmbitionBoxClient(client=http, delay=0, **options),
    ):
        report = capture_company_evidence(
            db, ('ambitionbox',), settings=config, clock=lambda: NOW
        )
    assert requests == [None]
    assert report['success'] is not public_fails, report
    if public_fails:
        with Store.open_readonly(db) as store:
            assert store.db.execute(
                'SELECT count(*) FROM company_evidence_snapshots'
            ).fetchone() == (0,)
    else:
        snapshot_id = report['outcomes'][0]['snapshot']['snapshot_id']
        exported = show_company_evidence(db, snapshot_ids=(snapshot_id,))
        responses = exported['snapshots'][0]['responses']
        assert len(responses) == 1
        raw = CompanyResponse.model_validate(responses[0])
        assert raw.body == public
        assert raw.body_sha256 == sha256(public).hexdigest()
