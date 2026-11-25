import json
from copy import deepcopy
from unittest.mock import patch

import pytest

from applicant.cli import main
from applicant.errors import Blocked, ConfigError
from applicant.infra.provider_registry import board_client
from applicant.services.research import import_research, read_research
from applicant.services.source_import import sample_import, validate_source_import


@pytest.mark.parametrize('provider', ['arc', 'otta'])
def test_restricted_public_route_never_constructs_network_client(provider):
    with (
        patch('applicant.infra.provider_registry.board_class') as adapter,
        pytest.raises(Blocked, match='authorized import'),
    ):
        board_client(provider)
    adapter.assert_not_called()


def test_import_probe_is_offline(tmp_path, capsys):
    database = tmp_path / 'new.db'
    assert (
        main(
            [
                'sources',
                'probe',
                '--provider',
                'levels',
                '--operation',
                'compensation',
                '--route',
                'import',
                '--store',
                'files',
                '--no-log-file',
                '--data-dir',
                str(tmp_path),
            ]
        )
        == 0
    )
    response = json.loads(capsys.readouterr().out)
    assert response['validation_kind'] == 'fixture'
    assert response['operation_exit_code'] == 0
    assert not database.exists()
    assert list(tmp_path.iterdir()) == []


def test_provider_input_probe_reads_manifest_without_opening_store(tmp_path, capsys):
    data = sample_import('nodeflair', 'compensation')
    source = tmp_path / 'compensation.json'
    source.write_text(json.dumps(data), encoding='utf-8')
    with patch('applicant.cli.sources.health') as database:
        code = main(
            [
                'sources',
                'probe',
                '--provider',
                'nodeflair',
                '--operation',
                'compensation',
                '--input',
                str(source),
                '--no-log-file',
                '--data-dir',
                str(tmp_path),
            ]
        )
    assert code == 0
    database.assert_not_called()
    assert json.loads(capsys.readouterr().out)['validation_kind'] == 'user_input'
    assert list(tmp_path.iterdir()) == [source]


def test_compensation_batch_preserves_identical_rows_and_reimports_idempotently(tmp_path):
    data = sample_import('nodeflair', 'compensation')
    data['manifest']['destinations'] = ['persist']
    first = data['bundle']['evidence'][0]
    data['bundle']['evidence'].append(deepcopy(first) | {'evidence_id': 'other-row'})
    database = tmp_path / 'observations.db'
    imported = validate_source_import(data)
    import_research(database, imported)
    assert len(read_research(database)['evidence']) == 2
    assert import_research(database, validate_source_import(data))['inserted'] == 0


def test_source_record_key_survives_changed_retrieval_time(tmp_path):
    data = sample_import('nodeflair', 'compensation')
    data['manifest']['destinations'] = ['persist']
    item = data['bundle']['evidence'][0]
    data['records'] = {item['evidence_id']: {'source_record_id': 'salary-record-1'}}
    database = tmp_path / 'observations.db'
    import_research(database, validate_source_import(data))
    item['acquired_at'] = '2026-11-25T08:00:00Z'
    assert import_research(database, validate_source_import(data))['inserted'] == 0
    item['payload']['minimum'] = '90000'
    assert import_research(database, validate_source_import(data))['inserted'] == 1


def test_restricted_import_requires_explicit_permission_and_destination():
    data = sample_import('levels', 'compensation')
    with pytest.raises(ConfigError, match='does not authorize'):
        validate_source_import(data)
    data['manifest']['destinations'] = ['persist']
    with pytest.raises(ConfigError, match='written permission'):
        validate_source_import(data)
    with pytest.raises(ConfigError, match='requires a provider import manifest'):
        validate_source_import(data['bundle'])
