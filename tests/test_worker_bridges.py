import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from applicant.errors import AuthFailed, Blocked, RateLimited, SourceError, Unparseable, Unreachable
from applicant.infra.board_worker import OwnedBoard, _collect
from applicant.infra.owned_worker import WorkerFailure
from applicant.services.research_feed import collect_feed
from tests.research_fixtures import NOW, bundle


class WorkerBridgesTest(unittest.TestCase):
    def test_owned_board_calls_and_translates_failures(self):
        job = bundle().observations[0].job
        board = OwnedBoard('builtin', Mock())
        client = Mock(
            raw={'listing': b'body'}, source_urls={'id': 'listing'}, requisition_metadata={}
        )
        client.search.return_value = [job]
        client.capture.return_value = [job]
        client.partial_jobs = [job]

        def inline(function, args, *, progress, **kwargs):
            connection = Mock()
            connection.send.side_effect = lambda packet: (
                progress(packet[1]) if packet[0] == 'progress' else None
            )
            return function(connection, args)

        with (
            patch('applicant.infra.board_worker.run_worker', side_effect=inline),
            patch('applicant.infra.board_worker.board_client', return_value=client),
        ):
            self.assertEqual(board.search('python'), [job])
            self.assertEqual(board.raw, {'listing': b'body'})
            client.raw = b'body'
            self.assertEqual(board.capture('listing'), [job])
            self.assertEqual(board.raw, b'body')
            client.capture.assert_called_once_with('listing')
            for error in (
                AuthFailed(),
                Blocked(),
                RateLimited('retry', retry_at=NOW),
                Unparseable(),
                Unreachable(),
                SourceError(),
            ):
                with self.subTest(error=type(error)), self.assertRaises(type(error)) as raised:
                    client.search.side_effect = error
                    board.search('python')
                self.assertEqual(board.partial_jobs, [job])
                if isinstance(error, RateLimited):
                    self.assertIsInstance(raised.exception, RateLimited)
                    assert isinstance(raised.exception, RateLimited)
                    self.assertEqual(raised.exception.retry_at, NOW)
            client.search.side_effect = None
            client.close.side_effect = WorkerFailure('teardown')
            with self.assertRaises(WorkerFailure):
                board.search('python')
            self.assertEqual(board.partial_jobs, [job])

    def test_constructor_failure_closes_protocol_without_client(self):
        connection = Mock()
        with patch('applicant.infra.board_worker.board_client', side_effect=Blocked):
            result = _collect(connection, ('builtin', 'search', (), {}))
        self.assertEqual(result['error']['type'], 'Blocked')
        connection.send.assert_called_once_with(('closing', None))

    def test_default_collection_preserves_worker_progress_on_teardown_failure(self):
        job = bundle().observations[0].job
        result = {
            'jobs': [job.model_dump()],
            'error': None,
            'raw': b'body',
            'source_urls': {},
            'requisition_metadata': {},
        }

        def failed(function, args, *, progress, **kwargs):
            progress(result)
            raise WorkerFailure('teardown')

        with (
            tempfile.TemporaryDirectory() as folder,
            patch('applicant.infra.board_worker.run_worker', side_effect=failed),
        ):
            snapshot = collect_feed(Path(folder) / 'db', {'providers': ['builtin']})
        self.assertEqual(snapshot['outcomes'][0]['status'], 'infrastructure_failure')
        self.assertEqual(snapshot['outcomes'][0]['preserved_count'], 1)
        self.assertEqual(snapshot['observations'][0]['content_representation'], 'http_response')
