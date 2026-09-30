"""Live PoP improvements that do not rely on closed-trade calibration."""
from __future__ import annotations

from datetime import timedelta

from utils import now_ist

from engine.live_expectation import (
    _chain_from_leg_marks,
    _pop_iv_stress_band,
    legs_from_fills,
    live_trade_outlook,
)


def _ic_legs():
    exp = now_ist().date() + timedelta(days=10)
    return [
        {"leg_order": 1, "action": "SELL", "strike": 23200.0, "option_type": "CE",
         "expiry_date": exp, "fill_price": 80.0, "lots": 1, "lot_size": 50},
        {"leg_order": 2, "action": "BUY", "strike": 23400.0, "option_type": "CE",
         "expiry_date": exp, "fill_price": 35.0, "lots": 1, "lot_size": 50},
        {"leg_order": 3, "action": "SELL", "strike": 22800.0, "option_type": "PE",
         "expiry_date": exp, "fill_price": 80.0, "lots": 1, "lot_size": 50},
        {"leg_order": 4, "action": "BUY", "strike": 22600.0, "option_type": "PE",
         "expiry_date": exp, "fill_price": 35.0, "lots": 1, "lot_size": 50},
    ]


class TestSkewChainAndBand:
    def test_chain_from_fills_when_no_ltps(self):
        sug = legs_from_fills(
            _ic_legs(), underlying="NIFTY",
            expiry=now_ist().date() + timedelta(days=10),
        )
        chain = _chain_from_leg_marks(sug, None)
        assert len(chain) == 4
        assert all(r["last_price"] > 0 for r in chain)

    def test_outlook_exposes_iv_band_and_net_ev(self):
        out = live_trade_outlook(
            legs=_ic_legs(), strategy="IRON_CONDOR",
            underlying="NIFTY", expiry=now_ist().date() + timedelta(days=10),
            spot=23000.0, dte=10, atm_iv=0.18,
            max_profit=4500.0, max_loss=15500.0, entry_pop=65.0,
        )
        assert out["live_pop"] is not None
        assert out["live_pop_lo"] is not None
        assert out["live_pop_hi"] is not None
        assert out["live_pop_lo"] <= out["live_pop"] <= out["live_pop_hi"]
        assert out["live_ev"] is not None
        assert out["est_charges_rs"] is not None and out["est_charges_rs"] > 0
        assert out["live_ev_net"] == round(out["live_ev"] - out["est_charges_rs"], 2)
        assert out["pop_uses_skew"] is True
        assert out["live_pop_hi"] - out["live_pop_lo"] >= 0.5
        assert "model" in (out.get("summary") or "").lower()

    def test_iv_stress_band_widens_with_vol_shock(self):
        sug = legs_from_fills(
            _ic_legs(), underlying="NIFTY",
            expiry=now_ist().date() + timedelta(days=10),
        )
        chain = _chain_from_leg_marks(sug, None)
        lo, hi = _pop_iv_stress_band(
            sug, 23000.0, 10, 0.18,
            chain=chain, strategy="IRON_CONDOR",
            include_pop=70.0,
        )
        assert lo is not None and hi is not None
        assert lo <= 70.0 <= hi
        assert hi >= lo
