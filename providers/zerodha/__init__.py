"""providers/zerodha — read-only market-data adapter for Zerodha Kite Connect.

This package is loaded only when `OPT_PROVIDERS=zerodha`. It uses the
official `kiteconnect` Python SDK (MIT license). The adapter is strictly
**market-data-only**: the `KiteFacade` exposes exactly three methods
(`set_access_token`, `instruments`, `ltp`) and nothing else. There is no
`place_order`, no `positions`, no `holdings` — they don't exist on the
facade at all.

Phase 2a scope (this package):
    - `facade.KiteFacade`        — minimal read-only wrapper around
                                   `kiteconnect.KiteConnect`.
    - `session`                  — daily access_token persistence (JSON file).
    - `rate_limiter.TokenBucket` — REST rate limiter (3 req/s `/quote`, 10/s `/ltp`).
    - `instruments.InstrumentMaster` — daily-refreshed instrument map.
    - `provider.ZerodhaProvider` — implements `MarketDataProvider`. EOD
                                   fallback wired in for historical / out-of-hours.

Phase 2b will add WebSocket (`KiteTicker`) — not in this package yet.
"""

from __future__ import annotations

from .provider import ZerodhaProvider
from .facade import KiteFacade
from .session import (
    ZerodhaSession,
    load_session,
    save_session,
    clear_session,
    is_token_valid,
)
from .ws_runner import (
    ConnState,
    KiteWSRunner,
    TokenMeta,
    WSStatus,
)
from .subscription_manager import (
    DEFAULT_INDEX_SPECS,
    IndexSpec,
    SubscriptionManager,
    SubscriptionStatus,
    make_db_leg_loader,
    make_static_leg_loader,
)

__all__ = [
    "ZerodhaProvider",
    "KiteFacade",
    "ZerodhaSession",
    "load_session",
    "save_session",
    "clear_session",
    "is_token_valid",
    "KiteWSRunner",
    "ConnState",
    "TokenMeta",
    "WSStatus",
    "SubscriptionManager",
    "SubscriptionStatus",
    "IndexSpec",
    "DEFAULT_INDEX_SPECS",
    "make_db_leg_loader",
    "make_static_leg_loader",
]
