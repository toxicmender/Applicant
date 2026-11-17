"""Lazy adapter construction lives outside the pure provider catalog."""

from importlib import import_module

from ..domain.providers import provider


def board_class(name: str):
    item = provider(name)
    if item.board is None:
        raise ValueError(f'{item.id}: acquisition unavailable; use research import')
    module, symbol = item.board.rsplit('.', 1)
    return getattr(import_module(module), symbol)


def board_client(name: str, **kwargs):
    adapter = board_class(name)
    if adapter.__module__ == 'applicant.boards.public':
        kwargs['name'] = provider(name).id
    return adapter(**kwargs)
