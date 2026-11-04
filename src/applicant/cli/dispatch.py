"""Bind argparse leaves to validated requests and typed service adapters."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from functools import partial
from typing import Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

from ..errors import ConfigError
from ..settings import Settings


class AddParser(Protocol):
    def __call__(self, name: str, *, help: str) -> argparse.ArgumentParser: ...


class Request(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True, validate_default=True)


R = TypeVar('R', bound=Request)
PLUMBING = {
    'command',
    'action',
    'handler',
    'prepare',
    'settings',
    'data_dir',
    'config',
    'store',
    'quiet',
    'verbose',
    'log_file',
    'no_log_file',
    'crunchbase_key',
    'tracxn_key',
}


def bind(
    parser: argparse.ArgumentParser,
    model: type[R],
    handler: Callable[[R, Settings], int],
) -> None:
    def prepare(args: argparse.Namespace, settings: Settings) -> Callable[[], int]:
        values = {name: value for name, value in vars(args).items() if name not in PLUMBING}
        try:
            request = model.model_validate(values)
        except ValidationError as error:
            # User inputs may include credentials. Render only field names and constraints.
            details = ', '.join(
                '{} ({})'.format(
                    '--' + '.'.join(map(str, item['loc'])).replace('_', '-')
                    if item['loc']
                    else 'options',
                    item['msg'],
                )
                for item in error.errors(include_input=False, include_context=False)
            )
            raise ConfigError(f'{parser.prog}: {details}') from None
        return partial(handler, request, settings)

    parser.set_defaults(handler=handler, prepare=prepare)
