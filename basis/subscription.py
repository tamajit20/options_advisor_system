"""WS subscription helpers for Cash-Futures Basis Monitor."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterable, List

from config import BASIS_CONFIG
from database.connection import SQLServerConnection
from basis.basis_engine import _coerce_date
from database.basis_models import BasisConfigRepo, BasisPairRepo

BasisPairLoader = Callable[[], Iterable["BasisSubscriptionPair"]]


@dataclass(frozen=True)
class BasisSubscriptionPair:
    symbol: str
    spot_token: int
    fut_token: int
    fut_expiry: date


def make_basis_pair_loader(db: SQLServerConnection) -> BasisPairLoader:
    """Return active basis_pairs for SubscriptionManager."""

    def _loader() -> List[BasisSubscriptionPair]:
        if not BASIS_CONFIG.get("enabled", True):
            return []
        cfg = BasisConfigRepo(db)
        if not cfg.get_enabled(default=BASIS_CONFIG.get("enabled", True)):
            return []
        rows = BasisPairRepo(db).list_active()
        out: List[BasisSubscriptionPair] = []
        for r in rows:
            try:
                exp = _coerce_date(r["fut_expiry"])
                if exp is None:
                    continue
                out.append(
                    BasisSubscriptionPair(
                        symbol=str(r["symbol"]).upper(),
                        spot_token=int(r["spot_token"]),
                        fut_token=int(r["fut_token"]),
                        fut_expiry=exp,
                    )
                )
            except (TypeError, ValueError, KeyError):
                continue
        return out

    return _loader
