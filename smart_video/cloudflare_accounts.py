"""MongoDB-backed Cloudflare multi-account rotation with UTC-day quota state."""

from datetime import datetime, timezone
import os

from .db import get_mongodb_collection

# Hard safety ceiling requested for routing. Cloudflare's free allocation is
# 10,000 neurons/day; stop selecting an account at 8,500.
NEURON_SWITCH_THRESHOLD = 8500.0
COLLECTION_NAME = os.getenv("CLOUDFLARE_USAGE_COLLECTION", "cloudflare_daily_usage")


def _today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _collection(client):
    return client[os.getenv("MONGODB_DATABASE", "storydb")][COLLECTION_NAME]


def _accounts_from_env():
    """Read every configured numbered account, preserving numeric order."""
    accounts = []
    # Do not require ACCOUNT_COUNT; this prevents an old count setting from
    # silently hiding configured accounts. Support up to 50 numbered pairs.
    for i in range(1, 51):
        account_id = os.getenv(f"CLOUDFLARE_ACCOUNT_ID_{i}", "").strip()
        token = os.getenv(f"CLOUDFLARE_API_TOKEN_{i}", "").strip()
        if account_id and token:
            accounts.append({"index": i, "account_id": account_id, "token": token})

    if not accounts:
        account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
        token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
        if account_id and token:
            accounts.append({"index": 1, "account_id": account_id, "token": token})
    return accounts


def ensure_indexes():
    client, _ = get_mongodb_collection()
    try:
        col = _collection(client)
        col.create_index(
            [("date_utc", 1), ("account_id", 1)],
            unique=True,
            name="cloudflare_account_day_unique",
        )
    finally:
        client.close()


def _ensure_account_day(account_id):
    day = _today_utc()
    now = datetime.now(timezone.utc)
    client, _ = get_mongodb_collection()
    try:
        _collection(client).update_one(
            {"date_utc": day, "account_id": account_id},
            {"$setOnInsert": {
                "date_utc": day,
                "account_id": account_id,
                "neurons_used": 0.0,
                "reserved_neurons": 0.0,
                "image_count": 0,
                "exhausted": False,
                "exhausted_reason": None,
                "created_at": now,
            }},
            upsert=True,
        )
    finally:
        client.close()


def get_account_status(account_id):
    day = _today_utc()
    _ensure_account_day(account_id)
    client, _ = get_mongodb_collection()
    try:
        row = _collection(client).find_one({"date_utc": day, "account_id": account_id}) or {}
        return {
            "neurons_used": max(0.0, float(row.get("neurons_used", 0.0) or 0.0)),
            "reserved_neurons": max(0.0, float(row.get("reserved_neurons", 0.0) or 0.0)),
            "exhausted": bool(row.get("exhausted", False)),
            "exhausted_reason": row.get("exhausted_reason"),
        }
    finally:
        client.close()


def get_account_statuses():
    """Return all configured accounts and today's UTC state."""
    ensure_indexes()
    day = _today_utc()
    accounts = _accounts_from_env()
    if not accounts:
        return []

    client, _ = get_mongodb_collection()
    try:
        col = _collection(client)
        rows = {d["account_id"]: d for d in col.find({"date_utc": day})}
        result = []
        for account in accounts:
            row = rows.get(account["account_id"], {})
            used = max(0.0, float(row.get("neurons_used", 0.0) or 0.0))
            reserved = max(0.0, float(row.get("reserved_neurons", 0.0) or 0.0))
            exhausted = bool(row.get("exhausted", False))
            result.append({
                **account,
                "date_utc": day,
                "neurons_used": used,
                "reserved_neurons": reserved,
                "exhausted": exhausted,
                "exhausted_reason": row.get("exhausted_reason"),
                "remaining_to_threshold": max(0.0, NEURON_SWITCH_THRESHOLD - used - reserved),
                "available": (not exhausted) and (used + reserved < NEURON_SWITCH_THRESHOLD),
            })
        return result
    finally:
        client.close()


def mark_account_exhausted(account_id, reason="CLOUDFLARE_429_4006"):
    """Mark account exhausted for this UTC day. It will not be selected again until the next UTC date."""
    day = _today_utc()
    now = datetime.now(timezone.utc)
    client, _ = get_mongodb_collection()
    try:
        _collection(client).update_one(
            {"date_utc": day, "account_id": account_id},
            {"$set": {
                "exhausted": True,
                "exhausted_reason": reason,
                "exhausted_at": now,
                "updated_at": now,
            }},
            upsert=True,
        )
    finally:
        client.close()
    print(f"🚫 Cloudflare account {account_id} marked EXHAUSTED for UTC day {day}: {reason}", flush=True)


def reserve_neurons(account_id, estimated_neurons):
    """Atomically reserve capacity only for a non-exhausted account."""
    day = _today_utc()
    value = max(0.0, float(estimated_neurons))
    now = datetime.now(timezone.utc)
    if value > NEURON_SWITCH_THRESHOLD:
        return False

    _ensure_account_day(account_id)
    client, _ = get_mongodb_collection()
    try:
        col = _collection(client)
        result = col.update_one(
            {
                "date_utc": day,
                "account_id": account_id,
                "exhausted": {"$ne": True},
                "$expr": {
                    "$lte": [
                        {"$add": [
                            {"$ifNull": ["$neurons_used", 0.0]},
                            {"$ifNull": ["$reserved_neurons", 0.0]},
                            value,
                        ]},
                        NEURON_SWITCH_THRESHOLD,
                    ]
                },
            },
            {"$inc": {"reserved_neurons": value}, "$set": {"updated_at": now}},
        )
        return result.modified_count == 1
    finally:
        client.close()


def release_reserved_neurons(account_id, estimated_neurons):
    """Release only the active reservation; never clear exhausted state."""
    day = _today_utc()
    value = max(0.0, float(estimated_neurons))
    now = datetime.now(timezone.utc)
    client, _ = get_mongodb_collection()
    try:
        _collection(client).update_one(
            {"date_utc": day, "account_id": account_id},
            {"$inc": {"reserved_neurons": -value}, "$set": {"updated_at": now}},
        )
    finally:
        client.close()


def settle_reserved_neurons(account_id, estimated_neurons, actual_neurons):
    day = _today_utc()
    estimated = max(0.0, float(estimated_neurons))
    actual = max(0.0, float(actual_neurons))
    delta = actual - estimated
    now = datetime.now(timezone.utc)
    client, _ = get_mongodb_collection()
    try:
        inc = {"reserved_neurons": -estimated, "neurons_used": actual}
        _collection(client).update_one(
            {"date_utc": day, "account_id": account_id},
            {"$inc": inc, "$set": {"updated_at": now}},
        )
    finally:
        client.close()


def record_image(account_id, neurons, source, story_id=None, part_no=None):
    day = _today_utc()
    now = datetime.now(timezone.utc)
    client, _ = get_mongodb_collection()
    try:
        inc = {"image_count": 1}
        if source == "reported":
            inc["reported_neurons"] = float(neurons)
        else:
            inc["estimated_neurons"] = float(neurons)
        item = {
            "story_id": story_id,
            "part_no": part_no,
            "neurons": float(neurons),
            "source": source,
            "at": now,
        }
        _collection(client).update_one(
            {"date_utc": day, "account_id": account_id},
            {"$inc": inc, "$push": {"recent_usage": {"$each": [item], "$slice": -100}}, "$set": {"updated_at": now}},
            upsert=True,
        )
    finally:
        client.close()


def choose_account(estimated_neurons, excluded_account_ids=None):
    """Reserve the first eligible account, skipping exhausted/previously tried accounts."""
    excluded = set(excluded_account_ids or set())
    for account in get_account_statuses():
        if account["account_id"] in excluded:
            continue
        if account["exhausted"]:
            print(f"⏭️ Skipping Cloudflare account {account['index']} - exhausted for {_today_utc()} UTC", flush=True)
            continue
        if reserve_neurons(account["account_id"], estimated_neurons):
            account["reserved_neurons"] += float(estimated_neurons)
            account["remaining_to_threshold"] = max(
                0.0,
                NEURON_SWITCH_THRESHOLD - account["neurons_used"] - account["reserved_neurons"],
            )
            return account
    return None
