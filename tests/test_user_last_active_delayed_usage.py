"""Delayed outbox delivery must not masquerade as a current user action."""
import uuid
import test_user_last_active as activity
import user_store

def test_delayed_usage_delivery_does_not_mark_offline_user_active_today():
    user = user_store.create_user("review-delayed-usage@x.com", "review-pass12345678")
    uid = user["user_id"]
    row = activity._sql("SELECT extract(epoch from now()-interval '10 days')::float8 AS t", fetch=True)
    occurred = row[0]["t"]
    tag = uuid.uuid4().hex
    activity._sql(
        "INSERT INTO ai_usage_events (event_id, call_id, payload_hash, schema_version, "
        "session_id, subject_type, subject_id, user_id, provider, model, "
        "occurred_at, enqueued_at, received_at, status) "
        "VALUES (%s,%s,%s,1,'review-session','user',%s,%s,'deepseek','m',"
        "to_timestamp(%s),to_timestamp(%s),now(),'unpriced')",
        ("evt_"+tag,"call_"+tag,"0"*64,uid,uid,occurred,occurred))
    activity._run_migration()
    last_active = activity._last_active(uid)
    assert last_active <= occurred+60, ("received_at is delivery time, not user activity",occurred,last_active)
