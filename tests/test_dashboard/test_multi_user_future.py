"""Future-scope stubs for multi-user login (parked).

See FUTURE_ENHANCEMENT_SCOPES.md → Platform — Multi-user login.
"""
from __future__ import annotations

import pytest


@pytest.mark.future
@pytest.mark.skip(
    reason="future: exactly one admin user "
           "(FUTURE_ENHANCEMENT_SCOPES.md → Platform — Multi-user login)",
)
def test_only_one_user_may_be_admin():
    """Promoting a second admin must demote the previous admin in one
    transaction; is_admin count stays exactly 1 among active users."""
    pass


@pytest.mark.future
@pytest.mark.skip(
    reason="future: ws_runner uses admin Zerodha session only "
           "(FUTURE_ENHANCEMENT_SCOPES.md → Platform — Multi-user login)",
)
def test_ws_runner_loads_admin_session_not_trader_session():
    """Tick / index / MTM feed must bind to the admin user's stored Zerodha
    token even when a non-admin user is logged into the dashboard."""
    pass


@pytest.mark.future
@pytest.mark.skip(
    reason="future: place/close uses logged-in user's Zerodha session "
           "(FUTURE_ENHANCEMENT_SCOPES.md → Platform — Multi-user login)",
)
def test_order_placement_uses_logged_in_user_zerodha_token():
    """ENTRY/EXIT/SUPPLEMENT and trading margin quotes must use the acting
    app user's token, never the admin feed token by default."""
    pass


@pytest.mark.future
@pytest.mark.skip(
    reason="future: trades and history scoped by owner_user_id "
           "(FUTURE_ENHANCEMENT_SCOPES.md → Platform — Multi-user login)",
)
def test_open_trades_and_history_are_private_per_user():
    """User A must not see User B's open trades, closed history, or
    trade-scoped notifications; suggestions remain shared."""
    pass


@pytest.mark.future
@pytest.mark.skip(
    reason="future: suggestion execute is per-user state "
           "(FUTURE_ENHANCEMENT_SCOPES.md → Platform — Multi-user login)",
)
def test_suggestion_execute_does_not_hide_card_for_other_users():
    """When User A executes a PENDING suggestion, User B still sees it as
    available via options_suggestion_user_state; shared IGNORED still retires
    for everyone."""
    pass


@pytest.mark.future
@pytest.mark.skip(
    reason="future: admin user list UI "
           "(FUTURE_ENHANCEMENT_SCOPES.md → Platform — Multi-user login)",
)
def test_admin_can_list_users_non_admin_cannot():
    """Admin-only Users API/UI lists app users; a non-admin caller gets 403."""
    pass


@pytest.mark.future
@pytest.mark.skip(
    reason="future: system/job/WS logs visible to all app users "
           "(FUTURE_ENHANCEMENT_SCOPES.md → Platform — Multi-user login)",
)
def test_admin_and_user_can_both_read_system_logs():
    """Non-admin may load system / jobs / WS feed logs; Zerodha execution
    logs remain own-orders-only unless product decision says otherwise."""
    pass
