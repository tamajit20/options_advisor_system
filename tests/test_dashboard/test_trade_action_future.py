"""Future-scope stubs for Trade Action Panel enhancements.

See FUTURE_ENHANCEMENT_SCOPES.md → Risk & Monitoring → Trade Action Panel.
"""
from __future__ import annotations

import pytest


@pytest.mark.future
@pytest.mark.skip(
    reason="future: live spot SL in action panel priority "
           "(FUTURE_ENHANCEMENT_SCOPES.md → Risk & Monitoring)",
)
def test_action_panel_uses_live_spot_sl_not_stale_alert():
    """When spot breaches SL level on tick, instruction must say CLOSE before
    MTM loss limit even if SL_TRIGGER notification is not yet in DB."""
    pass


@pytest.mark.future
@pytest.mark.skip(
    reason="future: leg-level buy/sell close lines on action panel "
           "(FUTURE_ENHANCEMENT_SCOPES.md → Risk & Monitoring)",
)
def test_action_panel_lists_per_leg_exit_with_ltp():
    """Instruction should include e.g. 'Buy back NIFTY 26000 PE @ ₹120' per
    open executed leg, not only 'close entire trade'."""
    pass


@pytest.mark.future
@pytest.mark.skip(
    reason="future: intraday per-leg SL in action panel "
           "(FUTURE_ENHANCEMENT_SCOPES.md → Risk & Monitoring)",
)
def test_action_panel_surfaces_intraday_leg_sl_breach():
    """When IntradayMonitor flags a short leg premium doubled, panel should
    instruct closing that leg even if whole-trade MTM is inside loss limit."""
    pass


@pytest.mark.future
@pytest.mark.skip(
    reason="future: multi-condition summary on action panel "
           "(FUTURE_ENHANCEMENT_SCOPES.md → Risk & Monitoring)",
)
def test_action_panel_shows_secondary_active_levels():
    """Primary verb stays one action; secondary breached levels listed under
    'Also active' when e.g. loss limit and profit floor both apply."""
    pass


@pytest.mark.future
@pytest.mark.skip(
    reason="future: level-events timeline API and trade card UI "
           "(FUTURE_ENHANCEMENT_SCOPES.md → Risk & Monitoring)",
)
def test_trade_card_renders_level_event_timeline():
    """GET /api/trades/<id>/level-events returns ENTER/EXIT rows and the
    trade card shows a collapsible whip-saw timeline."""
    pass
