"""Where applicant keeps things, and the keys it runs with - decided once.

Every module used to pick its own default path: `job_listing.json` here,
`.money_cache.json` there, the refreshed PPP table inside the installed package.
`Settings` is the one place those are decided, in this order - later wins:

1. defaults - everything in the current directory, as it always was
2. `applicant.toml` - in the current directory, or named by `--config` /
   `APPLICANT_CONFIG`
3. environment - `APPLICANT_HOME`, `APPLICANT_STORE`, `APPLICANT_MONEY_CACHE`,
   `CRUNCHBASE_API_KEY`, `TRACXN_API_KEY`
4. flags - `--data-dir`, `--store`, and each command's own file flags

A relative file name is relative to `data_dir`, so moving your data is one
setting. A relative `data_dir` is relative to the file that sets it - so
`applicant.toml` can say `data_dir = "data"` and mean the folder beside it - or,
from `--data-dir` or `APPLICANT_HOME`, to the working directory. API keys come from the environment or flags only - never from the
config file, which is the kind of thing that ends up committed - and are held
as `SecretStr`, so a stray `repr()` cannot put one in a log.

    # applicant.toml
    data_dir = "~/applicant"
    store = "sqlite"            # or "files": no database, the JSON/CSV only

    [files]
    listing = "job_listing.json"

    [searches.ai-ml]            # `applicant search --saved ai-ml`
    keywords = "ai ml engineer"
    title = ["ai", "ml", "machine learning"]
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError, field_validator

from . import log
from .errors import ConfigError

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on the 3.10 CI leg
    import tomli as tomllib  # pyright: ignore[reportMissingImports] - 3.10 only

CONFIG_FILE = 'applicant.toml'

Store = Literal['sqlite', 'files']
# the boards a saved search may name (applicant.services.search.SOURCES, and 'all')
SourceName = Literal['linkedin', 'indeed', 'naukri', 'googlejobs', 'all']


class Files(BaseModel):
    """What each file is called. Relative names live under `data_dir`."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    listing: str = 'job_listing.json'
    applications: str = 'applied_jobs.csv'
    reviews: str = 'company_reviews.json'
    financials: str = 'company_financials.json'
    money_cache: str = '.money_cache.json'
    # PPP factors refreshed here; the table shipped in the package is read-only
    ppp_factors: str = 'ppp_factors.json'
    logs: str = 'logs'
    cookies: str = 'cookies.json'
    glassdoor_profile: str = '.gd_profile'
    crunchbase_profile: str = '.cb_profile'
    tracxn_profile: str = '.tx_profile'


class SavedSearch(BaseModel):
    """A standing `applicant search`, kept in applicant.toml under [searches.<name>].

    Every field is a search flag, named as its long option (`--posted-within`
    is `posted_within`) and optional. Run it with `applicant search --saved
    <name>`; anything given on the command line wins over what is saved.

        [searches.ai-ml]
        keywords = "ai ml engineer"
        location = "India"
        title = ["ai", "ml", "machine learning", "data scientist"]
        experience = 3
        source = ["naukri", "linkedin"]
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    keywords: str | None = None
    location: str | None = None
    source: list[SourceName] | None = None
    limit: int | None = None
    want: int | None = None
    max_rounds: int | None = None
    output: str | None = None
    enrich: bool | None = None
    enrich_limit: int | None = None
    title: list[str] | None = None
    company: list[str] | None = None
    min_salary: float | None = None
    currency: str | None = None
    salary_basis: Literal['ppp', 'market', 'strict'] | None = None
    experience: float | None = None
    posted_within: int | None = None
    strict: bool | None = None
    strict_published: bool | None = None

    @field_validator('source', 'title', 'company', mode='before')
    @classmethod
    def _one_or_several(cls, value):
        """`title = "ai"` and `title = ["ai"]` mean the same."""
        return [value] if isinstance(value, str) else value


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    data_dir: Path = Path('.')
    # sqlite: applicant.db beside the files is the record and the JSON/CSV are
    # exports of it. files: the JSON/CSV alone, as before the database existed.
    store: Store = 'sqlite'
    files: Files = Files()
    crunchbase_key: SecretStr | None = None
    tracxn_key: SecretStr | None = None
    # standing searches, from [searches.<name>] tables in applicant.toml
    searches: dict[str, SavedSearch] = {}

    def path(self, name: str | os.PathLike[str]) -> str:
        """`name` under `data_dir`, unless it is absolute already.

        With the default `data_dir` of '.', a name comes back unchanged - which
        is why every path a run used before Settings existed is the same now.
        """
        candidate = Path(os.path.expanduser(os.fspath(name)))
        if candidate.is_absolute() or self.data_dir == Path('.'):
            return str(candidate)
        return str(self.data_dir / candidate)

    def secret(self, name: str) -> str | None:
        value = getattr(self, name)
        return value.get_secret_value() if value is not None else None

    @classmethod
    def load(
        cls,
        *,
        data_dir: str | os.PathLike[str] | None = None,
        store: str | None = None,
        config: str | os.PathLike[str] | None = None,
        environ: Mapping[str, str] | None = None,
        **keys: str | None,
    ) -> Settings:
        """Defaults, then the config file, then the environment, then these.

        `keys` takes `crunchbase_key` / `tracxn_key` from flags. A bad value
        anywhere is a ConfigError naming where it came from.
        """
        env = os.environ if environ is None else environ
        values: dict[str, Any] = {}

        path = config or env.get('APPLICANT_CONFIG')
        if path or Path(CONFIG_FILE).is_file():
            values.update(_read_config(Path(path or CONFIG_FILE), required=bool(path)))

        for key, variable in (('data_dir', 'APPLICANT_HOME'), ('store', 'APPLICANT_STORE')):
            if env.get(variable):
                values[key] = env[variable]
        if env.get('APPLICANT_MONEY_CACHE'):
            values['files'] = {
                **values.get('files', {}),
                'money_cache': env['APPLICANT_MONEY_CACHE'],
            }
        for key, variable in (
            ('crunchbase_key', 'CRUNCHBASE_API_KEY'),
            ('tracxn_key', 'TRACXN_API_KEY'),
        ):
            if env.get(variable):
                values[key] = env[variable]

        if data_dir is not None:
            values['data_dir'] = data_dir
        if store is not None:
            values['store'] = store
        values.update({key: value for key, value in keys.items() if value is not None})

        if 'data_dir' in values:
            values['data_dir'] = Path(os.path.expanduser(os.fspath(values['data_dir'])))
        try:
            settings = cls(**values)
        except ValidationError as error:
            raise ConfigError(f'invalid settings: {_describe(error)}') from None

        # masked in every log line from here on (ASVS 16.2.5)
        log.register_secret(settings.secret('crunchbase_key'))
        log.register_secret(settings.secret('tracxn_key'))
        return settings


def _read_config(path: Path, required: bool) -> dict[str, Any]:
    try:
        with open(path, 'rb') as handle:
            document = tomllib.load(handle)
    except FileNotFoundError:
        if required:
            raise ConfigError(f'config file {path} does not exist') from None
        return {}
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f'could not read {path}: {error}') from None

    secrets = {'crunchbase_key', 'tracxn_key'} & set(document)
    if secrets:
        raise ConfigError(
            f'{path} sets {", ".join(sorted(secrets))}: keys belong in the environment '
            '(CRUNCHBASE_API_KEY / TRACXN_API_KEY), not in a file that may be committed'
        )
    # a relative data_dir in a file means beside that file, wherever the command
    # is run from; on the command line it means the working directory, as usual
    data_dir = document.get('data_dir')
    if (
        isinstance(data_dir, str)
        and not data_dir.startswith('~')
        and not Path(data_dir).is_absolute()
    ):
        document['data_dir'] = str(path.parent / data_dir)
    return document


def _describe(error: ValidationError) -> str:
    return '; '.join(
        '{}: {}'.format('.'.join(str(part) for part in item['loc']), item['msg'])
        for item in error.errors()
    )
