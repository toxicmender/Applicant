import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from coverage import CoverageData
from tools.research_coverage_data import arcs, combine


class CoverageDataTest(unittest.TestCase):
    def test_cross_platform_union_and_incompatible_metadata(self):
        module = 'src/applicant/infra/owned_worker.py'
        expected = {'source_sha256': {module: 'fixture'}, 'commit': 'head'}
        with TemporaryDirectory() as folder:
            root = Path(folder)
            for os_name, source, edge in (
                ('Linux', '/home/runner/work/Applicant/Applicant/' + module, (1, 2)),
                ('Windows', 'D:\\a\\Applicant\\Applicant\\' + module.replace('/', '\\'), (2, 3)),
            ):
                artifact = root / os_name
                artifact.mkdir()
                data = CoverageData(basename=str(artifact / 'coverage-data'))
                data.add_arcs({source: [edge]})
                data.write()
                (artifact / 'coverage-provenance.json').write_text(
                    json.dumps({**expected, 'platform': os_name}),
                    encoding='utf-8',
                )
            output = str(root / '.coverage')
            combine(root, expected, output=output)
            result = CoverageData(basename=output)
            result.read()
            self.assertEqual(arcs(result, expected['source_sha256']), {module: {(1, 2), (2, 3)}})
            with self.assertRaisesRegex(ValueError, 'incompatible'):
                combine(root, {**expected, 'commit': 'other'}, output=output)

    def test_reject_unmapped_and_duplicate_paths(self):
        module = 'src/applicant/infra/owned_worker.py'
        data = CoverageData(no_disk=True)
        data.add_arcs({module: [(1, 2)], 'D:/a/Applicant/Applicant/' + module: [(2, 3)]})
        with self.assertRaisesRegex(ValueError, 'collision'):
            arcs(data, {module: 'fixture'})
        with self.assertRaisesRegex(ValueError, 'unmapped'):
            arcs(data, {})
