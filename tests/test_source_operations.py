import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from applicant.cli import main
from applicant.domain.providers import DEFAULT_BOARDS, PROVIDERS
from applicant.domain.source_operations import SourcesDocument
from applicant.errors import DatabaseBusy
from applicant.infra.owned_worker import CleanupUnconfirmed
from applicant.services.provider_health import acquire, finish


def test_catalog_operations_are_additive_and_offline(tmp_path, capsys):
    database = tmp_path / 'unused.db'
    assert main(['sources', 'list', '--database', str(database), '--json', '--no-log-file']) == 0
    result = json.loads(capsys.readouterr().out)
    SourcesDocument.model_validate(result)
    assert result['operations_schema_version'] == 1
    assert len(result['providers']) == 25
    items = {item['id']: item for item in result['providers']}
    assert items['linkedin']['authentication'] is None
    assert items['linkedin']['operations']['account_setup']['implementation'] == 'implemented'
    assert items['levels']['operations']['compensation']['implementation'] == 'import_only'
    assert items['flexa']['operations']['company_profile']['implementation'] == 'implemented'
    assert items['weworkremotely']['operations']['account_setup']['implementation'] == 'pending'
    assert DEFAULT_BOARDS == ('linkedin', 'indeed', 'naukri', 'googlejobs')
    assert all(item['operations'] for item in items.values())
    assert set(items) == set(PROVIDERS)
    assert not database.exists()


def test_checked_in_schema_matches_runtime_contract():
    schema = Path(__file__).resolve().parents[1] / 'docs' / 'source-operations.schema.json'
    assert json.loads(schema.read_text(encoding='utf-8')) == SourcesDocument.model_json_schema()


@pytest.mark.parametrize(
    ('status', 'exit_code'),
    [
        ('blocked', 3),
        ('throttled', 3),
        ('adapter_degraded', 5),
        ('needs_setup', 3),
        ('unavailable', 7),
        ('leased', 8),
    ],
)
def test_probes_preserve_failure_in_json(tmp_path, capsys, status, exit_code):
    args = [
        'sources',
        'probe',
        '--provider',
        'instahyre',
        '--no-log-file',
        '--database',
        str(tmp_path / 'unused.db'),
    ]
    value = {'job_version_ids': [], 'outcomes': [{'status': status}]}
    with patch('applicant.cli.sources.collect_feed', return_value=value):
        assert main(args) == 1
        legacy = json.loads(capsys.readouterr().out)
        assert main([*args, '--operation', 'job_search']) == exit_code
    assert legacy == json.loads(capsys.readouterr().out)
    assert legacy['reason'] == status
    assert legacy['operation_exit_code'] == exit_code


@pytest.mark.parametrize(('error', 'code'), [(DatabaseBusy, 8), (CleanupUnconfirmed, 1)])
def test_probe_reports_worker_and_sqlite_failures(tmp_path, capsys, error, code):
    with patch('applicant.cli.sources.collect_feed', side_effect=error('synthetic failure')):
        assert (
            main(
                [
                    'sources',
                    'probe',
                    '--provider',
                    'instahyre',
                    '--operation',
                    'job_search',
                    '--database',
                    str(tmp_path / 'unused.db'),
                    '--no-log-file',
                ]
            )
            == code
        )
    assert json.loads(capsys.readouterr().out)['operation_exit_code'] == code


def test_detail_probe_uses_only_explicit_locator(tmp_path, capsys):
    database = tmp_path / 'unused.db'
    path = tmp_path / 'selection.json'
    url = 'https://builtin.com/job/example/42'
    path.write_text(json.dumps({'url': url}), encoding='utf-8')
    with patch(
        'applicant.cli.sources.collect_feed',
        return_value={
            'job_version_ids': ['job-id'],
            'outcomes': [{'status': 'success'}],
        },
    ) as collect:
        assert (
            main(
                [
                    'sources',
                    'probe',
                    '--provider',
                    'builtin',
                    '--operation',
                    'job_detail',
                    '--database',
                    str(database),
                    '--input',
                    str(path),
                    '--no-log-file',
                ]
            )
            == 0
        )
    assert collect.call_args.args[1]['urls'] == {'builtin': [url]}
    assert json.loads(capsys.readouterr().out)['operation'] == 'job_detail'


def test_runtime_projects_quarantine_and_cooldown(tmp_path, capsys):
    path = tmp_path / 'health.db'
    route = ('job_search', 'public', '1')
    lease = acquire(path, 'weworkremotely')
    finish(path, 'weworkremotely', lease['token'], 'adapter_degraded', route=route)
    common = ['--database', str(path), '--no-log-file']
    assert main(['sources', 'show', 'wwr', *common]) == 0
    output = json.loads(capsys.readouterr().out)
    assert (
        output['providers'][0]['operations']['job_search']['routes'][0]['runtime']['reason']
        == 'parser_quarantined'
    )
    lease = acquire(path, 'weworkremotely')
    finish(path, 'weworkremotely', lease['token'], 'throttled')
    with patch('applicant.cli.sources.collect_feed') as collector:
        assert (
            main(['sources', 'probe', '--provider', 'wwr', '--operation', 'job_search', *common])
            == 3
        )
    collector.assert_not_called()
    assert json.loads(capsys.readouterr().out)['eligible_at']


def test_account_probe_preserves_challenge_code(tmp_path, capsys):
    state = tmp_path / 'synthetic.json'
    state.touch()
    with (
        patch('applicant.cli.sources.provider_session', return_value=SimpleNamespace(path=state)),
        patch(
            'applicant.cli.sources.session_status',
            side_effect=[{'status': 'verified'}, {'status': 'inconclusive', 'reason': 'challenge'}],
        ),
    ):
        assert (
            main(
                [
                    'sources',
                    'probe',
                    '--provider',
                    'indeed',
                    '--operation',
                    'account_setup',
                    '--store',
                    'files',
                    '--no-log-file',
                ]
            )
            == 11
        )
    assert json.loads(capsys.readouterr().out)['reason'] == 'challenge'


@pytest.mark.parametrize(
    'options',
    [
        ['--route', 'public'],
        ['--operation', 'job_detail', '--route', 'does_not_exist'],
        ['--operation', 'job_detail'],
    ],
)
def test_invalid_probe_exits_before_adapter(tmp_path, options):
    with patch('applicant.cli.sources.collect_feed') as collect:
        assert (
            main(
                [
                    'sources',
                    'probe',
                    '--provider',
                    'wellfound',
                    '--database',
                    str(tmp_path / 'test.db'),
                    '--no-log-file',
                    *options,
                ]
            )
            == 2
        )
    collect.assert_not_called()


def test_route_pin_does_not_fall_back_or_start_a_worker(tmp_path, capsys):
    with patch('applicant.cli.sources.collect_feed') as collect:
        code = main(
            [
                'sources',
                'probe',
                '--provider',
                'otta',
                '--operation',
                'job_search',
                '--route',
                'public',
                '--database',
                str(tmp_path / 'unused.db'),
                '--no-log-file',
            ]
        )
    assert code == 2
    collect.assert_not_called()
    result = json.loads(capsys.readouterr().out)
    assert result['route'] == 'public'
    assert result['reason'] == 'prohibited'


@pytest.mark.parametrize(('explicit', 'code'), [(False, 1), (True, 0)])
def test_probes_share_json_for_recognized_empty_search(tmp_path, capsys, explicit, code):
    value = {
        'job_version_ids': [],
        'outcomes': [{'provider': 'weworkremotely', 'status': 'success'}],
    }
    argv = [
        'sources',
        'probe',
        '--provider',
        'wwr',
        '--database',
        str(tmp_path / 'unused.db'),
        '--json',
        '--no-log-file',
    ]
    if explicit:
        argv += ['--operation', 'job_search']
    with patch('applicant.cli.sources.collect_feed', return_value=value):
        assert main(argv) == code
    result = json.loads(capsys.readouterr().out)
    assert result['job_version_ids'] == []
    assert result['operation'] == 'job_search'
    assert result['route'] == 'public'
    assert result['status'] == 'empty'
    assert result['operation_exit_code'] == 0
