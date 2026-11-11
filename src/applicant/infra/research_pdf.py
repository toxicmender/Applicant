"""Latin-first ReportLab rendering with full preflight and atomic replacement."""

import os
import tempfile
import unicodedata
from html import escape
from importlib import resources
from pathlib import Path

from ..domain.canonical import content_hash
from ..errors import ConfigError
from ..permissions import make_private


def render_pdf(document, target: Path, *, missing_glyph='escape'):
    try:
        from fontTools.ttLib import TTFont as FontInfo
        from reportlab import Version
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle
    except ImportError:
        raise ConfigError('PDF support requires applicant[pdf]; run uv sync --extra pdf') from None
    font = resources.files('applicant').joinpath('assets/fonts/NotoSans-Regular.ttf')
    diagnostics = []
    with resources.as_file(font) as path:
        with FontInfo(str(path)) as info:
            chars = set(info.getBestCmap() or {})
            font_version = info['name'].getDebugName(5)
        pdfmetrics.registerFont(TTFont('ApplicantNoto', str(path)))

    def safe(value, location):
        result = []
        for char in unicodedata.normalize('NFC', str(value)):
            code = ord(char)
            supported_script = (
                code < 128
                or 'LATIN' in unicodedata.name(char, '')
                or unicodedata.category(char) in ('Sc', 'Pd', 'Ps', 'Pe', 'Pi', 'Pf', 'Po', 'Zs')
            )
            if char in '\n\t' or (code in chars and supported_script and code >= 32):
                result.append(char)
            else:
                replacement = f'\\u{code:04x}' if code <= 0xFFFF else f'\\U{code:08x}'
                diagnostics.append(
                    {'field': location, 'codepoint': f'U+{code:04X}', 'replacement': replacement}
                )
                result.append(replacement)
        return ''.join(result)

    title = safe(document['title'], 'title')
    sections = []
    for i, section in enumerate(document['sections']):
        sections.append(
            {
                'title': safe(section['title'], f'sections[{i}].title'),
                'note': safe(section.get('note', ''), f'sections[{i}].note'),
                'columns': [safe(x, f'sections[{i}].columns') for x in section['columns']],
                'rows': [
                    [safe(x, f'sections[{i}].rows[{j}][{k}]') for k, x in enumerate(row)]
                    for j, row in enumerate(section['rows'])
                ],
            }
        )
    if diagnostics and missing_glyph == 'error':
        raise ConfigError(
            f'unsupported PDF text at {diagnostics[0]["field"]}: {diagnostics[0]["codepoint"]}; JSON/Markdown preserve the original'
        )
    style = ParagraphStyle(
        'Body',
        fontName='ApplicantNoto',
        fontSize=8,
        leading=11,
        alignment=TA_LEFT,
        wordWrap='LTR',
        splitLongWords=True,
    )
    heading = ParagraphStyle(
        'Heading', parent=style, fontSize=14, leading=18, spaceAfter=10, keepWithNext=True
    )

    def paragraph(value):
        text = escape(value).replace('\n', '<br/>')
        if value.startswith('https://') and not any(c.isspace() for c in value):
            text = f'<link href="{escape(value, quote=True)}" color="#1e40af">{text}</link>'
        return Paragraph(text, style)

    story = [
        Paragraph(escape(title), heading),
        paragraph('Generated: ' + document['created_at']),
        Spacer(1, 12),
    ]
    if diagnostics:
        story.extend(
            [
                paragraph(
                    'Unsupported-script text is shown as reversible Unicode escapes. Original text is preserved in the JSON/Markdown report.'
                ),
                Spacer(1, 10),
            ]
        )
    for section in sections:
        story.extend([Paragraph(escape(section['title']), heading)])
        if section['note']:
            story.append(paragraph(section['note']))
        cells = [
            [paragraph(x) for x in section['columns']],
            *[[paragraph(x) for x in row] for row in section['rows']],
        ]
        count = len(section['columns'])
        widths = [90, 105, 205, 123] if count == 4 else [523 / count] * count
        table = LongTable(cells, colWidths=widths, repeatRows=1, splitInRow=1)
        table.setStyle(
            TableStyle(
                [
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e5eef8')),
                    ('GRID', (0, 0), (-1, -1), 0.3, colors.HexColor('#cbd5e1')),
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ('LEFTPADDING', (0, 0), (-1, -1), 6),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 6),
                ]
            )
        )
        story.extend([table, Spacer(1, 16)])
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix='.' + target.name, suffix='.pdf', dir=target.parent
    )
    os.close(descriptor)
    make_private(temporary)
    try:

        def footer(canvas, doc):
            canvas.setFont('ApplicantNoto', 8)
            canvas.drawRightString(559, 20, f'Page {doc.page}')

        doc = SimpleDocTemplate(
            temporary,
            pagesize=(595, 842),
            leftMargin=36,
            rightMargin=36,
            topMargin=36,
            bottomMargin=36,
        )
        doc.build(story, onFirstPage=footer, onLaterPages=footer)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {
        'report_hash': content_hash(document),
        'template_version': 'report-1',
        'font': 'NotoSans-Regular',
        'font_version': font_version,
        'font_hash': content_hash(font.read_bytes().hex()),
        'reportlab_version': Version,
        'substitutions': diagnostics,
    }
