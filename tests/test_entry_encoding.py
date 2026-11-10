"""The executable owns encoding; embedded callers own their streams."""

import os
import subprocess
import sys
import unittest


class EntryEncodingTest(unittest.TestCase):
    def child(self, code, encoding='cp1252:strict'):
        return subprocess.run(
            [sys.executable, '-c', code],
            env={**os.environ, 'PYTHONUTF8': '0', 'PYTHONIOENCODING': encoding},
            capture_output=True,
            check=True,
        ).stdout

    def test_embedded_main_preserves_stream(self):
        output = self.child(
            'import sys; from applicant.cli import main; '
            "before=sys.stdout.encoding; main(['sources','list','--no-log-file']); "
            'assert sys.stdout.encoding==before; print(chr(233))'
        )
        self.assertTrue(output.endswith(b'\xe9\r\n') or output.endswith(b'\xe9\n'))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows executable contract')
    def test_entrypoint_outputs_utf8_despite_inherited_encoding(self):
        for encoding in ('cp1252:strict', 'utf-8'):
            with self.subTest(encoding=encoding):
                output = self.child(
                    'import sys; from applicant.cli import entrypoint; '
                    "sys.argv=['applicant','sources','list','--no-log-file']; "
                    'entrypoint(); print(chr(0x20b9)+chr(233))',
                    encoding,
                )
                self.assertIn('\u20b9é', output.decode('utf-8'))
