"""Independent acceptance assertions: expected product invariants, not current behavior."""
from datetime import datetime, timedelta, timezone
import test_credit_billing as base

_isolated = base._isolated

@base.pytest.mark.parametrize("frozen", [False, True])
def test_missing_hold_must_not_silently_debit_wallet(frozen):
    base._seed_prices()
    user, account = base._user()
    if frozen:
        conn = base.ch.connect()
        try:
            with base.ch.tx(conn) as cur:
                cur.execute("UPDATE billing_accounts SET credit_frozen_reason='payment_reversal', credit_frozen_at=now() WHERE account_id=%s", (account["account_id"],))
        finally:
            conn.close()
    call_id, session_id, request_id = base._ids()
    base.bh.bind_run_binding(request_id, session_id, "user", user["user_id"])
    event = base.ch.usage_event(call_id, session_id, user["user_id"],
        occurred=base.T0, request_id=request_id, user_id=user["user_id"])
    before = base._account(account["account_id"])["credit_balance_units"]
    result = base._ingest(event)
    after = base._account(account["account_id"])["credit_balance_units"]
    assert after == before, (result["credit_settlement"], before, after)

def test_post_migration_run_cannot_be_legacy_only_because_client_time_is_earlier():
    base._seed_prices()
    user = base._legacy_user("independent-migration")
    account = base.ch.provision_user(user["user_id"], grant_units=5*base.CNY,
        kind="legacy_grant", migration={"legacy_grant_units": 5*base.CNY},
        source_key="legacy:%s:v1" % user["user_id"])
    call_id, session_id, request_id = base._ids()
    # This trusted run binding is created AFTER the migration.
    base.bh.bind_run_binding(request_id, session_id, "user", user["user_id"])
    event = base.ch.usage_event(call_id, session_id, user["user_id"],
        occurred=account["credit_migrated_at"] - timedelta(hours=1),
        request_id=request_id, user_id=user["user_id"])
    result = base._ingest(event, now=datetime.now(timezone.utc))
    assert result["billing_backend"] == "credits", result["billing_backend"]
