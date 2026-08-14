"""WS subscription helpers for Arb Monitor (dual-listed NSE+BSE pairs)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, List

from config import ARB_CONFIG
from database.connection import SQLServerConnection
from database.arb_models import ArbConfigRepo, ArbPairRepo

ArbPairLoader = Callable[[], Iterable["ArbSubscriptionPair"]]


@dataclass(frozen=True)
class ArbSubscriptionPair:
    symbol: str
    nse_token: int
    bse_token: int


def make_arb_pair_loader(db: SQLServerConnection) -> ArbPairLoader:
    """Return active arb_pairs for SubscriptionManager."""

    def _loader() -> List[ArbSubscriptionPair]:
        if not ARB_CONFIG.get("enabled", True):
            return []
        cfg = ArbConfigRepo(db)
        if not cfg.get_enabled(default=ARB_CONFIG.get("enabled", True)):
            return []
        rows = ArbPairRepo(db).list_active()
        out: List[ArbSubscriptionPair] = []
        for r in rows:
            try:
                out.append(
                    ArbSubscriptionPair(
                        symbol=str(r["symbol"]).upper(),
                        nse_token=int(r["nse_token"]),
                        bse_token=int(r["bse_token"]),
                    )
                )
            except (TypeError, ValueError, KeyError):
                continue
        return out

    return _loader
