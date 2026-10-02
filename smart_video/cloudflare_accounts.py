"""MongoDB-backed Cloudflare account rotation and daily neuron ledger."""
from datetime import datetime, timezone
import os
import time

from .db import get_mongodb_collection

NEURON_SWITCH_THRESHOLD = float(os.getenv("CLOUDFLARE_ACCOUNT_SWITCH_THRESHOLD", "8300"))
COLLECTION_NAME = os.getenv("CLOUDFLARE_USAGE_COLLECTION", "cloudflare_daily_usage")


def _accounts_from_env():
    accounts = []
    count = int(os.getenv("CLOUDFLARE_ACCOUNT_COUNT", "3"))
    for i in range(1, count + 1):
        account_id = os.getenv(f"CLOUDFLARE_ACCOUNT_ID_{i}", "").strip()
        token = os.getenv(f"CLOUDFLARE_API_TOKEN_{i}", "").strip()
        if account_id and token:
            accounts.append({"index": i, "account_id": account_id, "token": token})
    # Backward compatibility with the old single-account variables.
    if not accounts:
        account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
        token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
        if account_id and token:
            accounts.append({"index": 1, "account_id": account_id, "token": token})
    return accounts


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _collection(client):
    # get_mongodb_collection() returns the longstory collection. Use its database.
    return client[os.getenv("MONGODB_DATABASE", "storydb")][COLLECTION_NAME]


def ensure_indexes():
    client, _ = get_mongodb_collection()
    try:
        col = _collection(client)
        col.create_index([("date_utc", 1), ("account_id", 1)], unique=True)
        col.create_index([("date_utc", 1), ("neurons_used", 1)])
    finally:
        client.close()


def get_account_statuses():
    day = _today()
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
            used = float(row.get("neurons_used", 0.0))
            result.append({
                **account,
                "date_utc": day,
                "neurons_used": used,
                "remaining_to_threshold": max(0.0, NEURON_SWITCH_THRESHOLD - used),
                "available": used <= NEURON_SWITCH_THRESHOLD,
            })
        return result
    finally:
        client.close()


def reserve_neurons(account_id, estimated_neurons):
    """Atomically reserve estimated neurons only when daily usage stays <= threshold."""
    day = _today()
    value = max(0.0, float(estimated_neurons))
    client, _ = get_mongodb_collection()
    try:
        col = _collection(client)
        now = datetime.now(timezone.utc)
        # Create today's row first; then reserve atomically.
        col.update_one(
            {"date_utc": day, "account_id": account_id},
            {"$setOnInsert": {"date_utc": day, "account_id": account_id, "neurons_used": 0.0, "created_at": now}},
            upsert=True,
        )
        query = {
            "date_utc": day,
            "account_id": account_id,
            "$expr": {"$lte": [{"$add": [{"$ifNull": ["$neurons_used", 0]}, value]}, NEURON_SWITCH_THRESHOLD]},
        }
        update = {
            "$setOnInsert": {
                "date_utc": day,
                "account_id": account_id,
                "created_at": now,
            },
            "$inc": {"neurons_used": value, "reserved_neurons": value},
            "$set": {"updated_at": now},
        }
        result = col.update_one(query, update, upsert=False)
        if result.modified_count == 1 or result.upserted_id is not None:
            return True
        return False
    finally:
        client.close()


def release_reserved_neurons(account_id, estimated_neurons):
    """Release a reservation when an image request failed before producing an image."""
    value = max(0.0, float(estimated_neurons))
    client, _ = get_mongodb_collection()
    try:
        col = _collection(client)
        col.update_one(
            {"date_utc": _today(), "account_id": account_id},
            {"$inc": {"neurons_used": -value, "reserved_neurons": -value},
             "$set": {"updated_at": datetime.now(timezone.utc)}},
        )
    finally:
        client.close()


def adjust_neurons(account_id, estimated_neurons, reported_neurons):
    """Adjust the ledger if Cloudflare reports a different neuron count."""
    delta = float(reported_neurons) - float(estimated_neurons)
    if abs(delta) < 0.0001:
        return
    client, _ = get_mongodb_collection()
    try:
        col = _collection(client)
        col.update_one(
            {"date_utc": _today(), "account_id": account_id},
            {"$inc": {"neurons_used": delta},
             "$set": {"updated_at": datetime.now(timezone.utc)}},
        )
    finally:
        client.close()


def record_image(account_id, neurons, source, story_id=None, part_no=None):
    client, _ = get_mongodb_collection()
    try:
        col = _collection(client)
        now = datetime.now(timezone.utc)
        inc = {"image_count": 1}
        if source == "reported":
            inc["reported_neurons"] = float(neurons)
        else:
            inc["estimated_neurons"] = float(neurons)
        col.update_one(
            {"date_utc": _today(), "account_id": account_id},
            {"$inc": inc, "$set": {"updated_at": now}, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        if story_id is not None:
            col.update_one(
                {"date_utc": _today(), "account_id": account_id},
                {"$push": {"recent_usage": {"story_id": story_id, "part_no": part_no, "neurons": float(neurons), "source": source, "at": now}}},
            )
    finally:
        client.close()


def choose_account(estimated_neurons):
    """Pick the first account with enough remaining daily capacity."""
    ensure_indexes()
    for account in get_account_statuses():
        if reserve_neurons(account["account_id"], estimated_neurons):
            return account
    return None
