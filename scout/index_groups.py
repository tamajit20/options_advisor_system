"""Index membership tags for Scout watchlist grouping (Nifty 50, Nifty Bank, …)."""

from __future__ import annotations

from typing import Dict, List

from config import NIFTY_50_SYMBOLS, NIFTY_BANK_SYMBOLS

INDEX_GROUPS: Dict[str, dict] = {
    "nifty50": {"label": "Nifty 50", "badge": "50", "order": 0},
    "nifty_bank": {"label": "Nifty Bank", "badge": "BN", "order": 1},
}

_SYMBOL_TAGS: Dict[str, List[str]] = {}
_NIFTY50_RANK: Dict[str, int] = {s.upper(): i for i, s in enumerate(NIFTY_50_SYMBOLS)}
_NIFTY_BANK_RANK: Dict[str, int] = {s.upper(): i for i, s in enumerate(NIFTY_BANK_SYMBOLS)}


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
    sym = str(row.get("symbol", "")).upper()
    tags = row.get("index_tags") or index_tags(sym)
    if "nifty50" in tags:
        return (0, _NIFTY50_RANK.get(sym, 9999), sym)
    if "nifty_bank" in tags:
        return (1, _NIFTY_BANK_RANK.get(sym, 9999), sym)
    name = str(row.get("name") or "").strip().upper()
    return (2, name or sym, sym)


def sort_watchlist_rows(rows: List[dict]) -> List[dict]:
    return sorted(rows, key=watchlist_sort_key)


def nifty_bank_symbols() -> List[str]:
    return list(NIFTY_BANK_SYMBOLS)
