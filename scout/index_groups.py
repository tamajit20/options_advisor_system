"""Index membership tags for Scout watchlist grouping (Nifty 50, Nifty Bank, …)."""

from __future__ import annotations

from typing import Dict, List

from config import NIFTY_50_SYMBOLS, NIFTY_BANK_SYMBOLS

INDEX_GROUPS: Dict[str, dict] = {
    "nifty50": {"label": "Nifty 50", "badge": "50", "order": 0},
    "nifty_bank": {"label": "Nifty Bank", "badge": "BN", "order": 1},
}

_SYMBOL_TAGS: Dict[str, List[str]] = {}


def _build_symbol_tags() -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for sym in NIFTY_50_SYMBOLS:
        out.setdefault(sym.upper(), []).append("nifty50")
    for sym in NIFTY_BANK_SYMBOLS:
        tag = "nifty_bank"
        if tag not in out.setdefault(sym.upper(), []):
            out[sym.upper()].append(tag)
    for tags in out.values():
        tags.sort(key=lambda t: INDEX_GROUPS[t]["order"])
    return out


def index_tags(symbol: str) -> List[str]:
    global _SYMBOL_TAGS
    if not _SYMBOL_TAGS:
        _SYMBOL_TAGS = _build_symbol_tags()
    return list(_SYMBOL_TAGS.get(str(symbol).upper(), []))


def watchlist_sort_key(row: dict) -> tuple:
    tags = row.get("index_tags") or index_tags(row.get("symbol", ""))
    if "nifty50" in tags:
        tier = 0
    elif "nifty_bank" in tags:
        tier = 1
    else:
        tier = 2
    return (tier, row.get("symbol", ""))


def sort_watchlist_rows(rows: List[dict]) -> List[dict]:
    return sorted(rows, key=watchlist_sort_key)


def nifty_bank_symbols() -> List[str]:
    return list(NIFTY_BANK_SYMBOLS)
