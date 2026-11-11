"""Lazy adapter construction lives outside the pure provider catalog."""

from importlib import import_module

from ..domain.providers import provider


def board_client(name: str, **kwargs):
    item = provider(name)
    if item.board is None:
        raise ValueError(f'{item.id}: acquisition unavailable; use research import')
    module, symbol = item.board.rsplit('.', 1)
    if module == 'applicant.boards.public':
        kwargs['name'] = item.id
    return getattr(import_module(module), symbol)(**kwargs)
