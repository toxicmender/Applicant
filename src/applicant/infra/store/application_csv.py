"""Validate and preserve the layout of an application CSV during projection."""

import codecs
import csv
import io
import re
from collections import Counter
from pathlib import Path
from typing import Literal

from ...domain.application_cells import APPLIED_COLUMNS, neutralise, restore
from ...errors import ExportConflict
from .publication import read_bytes

_CELL = re.compile(r'"(?:[^"]|"")*"|[^",\r\n]*')


class ApplicationCSV:
    def __init__(self, raw):
        self.bom = raw is None or raw.startswith(codecs.BOM_UTF8)
        self.header = list(APPLIED_COLUMNS)
        self.terminator = '\r\n'
        self.quoting: Literal[0, 1] = csv.QUOTE_ALL if raw is None else csv.QUOTE_MINIMAL
        self.cells = []
        if raw is None:
            return
        source = raw.decode('utf-8-sig')
        if not source.strip():
            return
        records = iter(_records(source))
        header, quoted, self.terminator = next(records)
        self.header = header
        if any(count > 1 and name in APPLIED_COLUMNS for name, count in Counter(header).items()):
            raise ValueError('duplicate application CSV column')
        if not all(header.count(column) == 1 for column in ('status', 'source', 'id', 'note')):
            raise ValueError('application CSV is missing unique required columns')
        if all(flag for value, flag in zip(header, quoted, strict=True) if value.strip()):
            self.quoting = csv.QUOTE_ALL
        for cells, _, terminator in records:
            if len(cells) != len(header):
                raise ValueError('invalid application CSV row width')
            if terminator and terminator != self.terminator:
                raise ValueError('mixed application CSV record terminators')
            self.cells.append(cells)
        self.terminator = self.terminator or '\r\n'

    @classmethod
    def read(cls, path: str | Path):
        target = Path(path)
        try:
            return cls(read_bytes(target))
        except ValueError as error:
            raise ExportConflict(f'{target}: invalid application CSV: {error}') from error

    def rows(self):
        counts = Counter(self.header)
        named = [
            (index, name)
            for index, name in enumerate(self.header)
            if name.strip() and counts[name] == 1
        ]
        return [{name: restore(row[index]) for index, name in named} for row in self.cells]

    def set(self, index, changes):
        if index is None:
            self.cells.append([neutralise(changes.get(column)) for column in self.header])
        else:
            for column, value in changes.items():
                self.cells[index][self.header.index(column)] = neutralise(value)

    def render(self):
        output = io.StringIO(newline='')
        writer = csv.writer(output, quoting=self.quoting, lineterminator=self.terminator)
        writer.writerow(self.header)
        writer.writerows(self.cells)
        return output.getvalue().encode('utf-8-sig' if self.bom else 'utf-8')


def _records(source):
    position = 0
    while position < len(source):
        fields, quoted = [], []
        while True:
            match = _CELL.match(source, position)
            assert match is not None
            token = match[0]
            is_quoted = token.startswith('"')
            fields.append(token[1:-1].replace('""', '"') if is_quoted else token)
            quoted.append(is_quoted)
            position = match.end()
            if source[position : position + 1] != ',':
                break
            position += 1
        if position == len(source):
            yield fields, quoted, ''
            return
        if source.startswith('\r\n', position):
            terminator = '\r\n'
        elif source.startswith('\n', position):
            terminator = '\n'
        else:
            raise ValueError('malformed application CSV record')
        yield fields, quoted, terminator
        position += len(terminator)
