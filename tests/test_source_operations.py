import json
from unittest.mock import patch

import pytest

from applicant.cli import main
from applicant.domain.providers import DEFAULT_BOARDS, PROVIDERS


def test_catalog_operations_are_additive_and_offline(tmp_path, capsys):
    database = tmp_path / 'unused.db'
    assert main(['sources', 'list', '--database', str(database), '--json', '--no-log-file']) == 0
    result = json.loads(capsys.readouterr().out)
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
