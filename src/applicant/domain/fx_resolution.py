"""Recorded result of new-run FX selection; historical loads never resolve anew."""

from .contracts import CurrencyCode, Identity
from .fx import FXSnapshot
from .ranking_v2 import EngineContract, FXSnapshotV2


class FXResolution(EngineContract):
    snapshot: FXSnapshot | FXSnapshotV2
    diagnostics: tuple[Identity, ...] = ()
    missing_currencies: tuple[CurrencyCode, ...] = ()
