"""MongoDB connection and resumable long-story/part status management."""

import time
from datetime import datetime, timezone

from pymongo import MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure

from .config import (
    MONGODB_URI,
    STORY_ID,
    DATABASE_NAME,
    COLLECTION_NAME,
    MONGODB_SERVER_TIMEOUT_MS,
)

PART_STATUSES = {"PENDING", "PROCESSING", "SUCCESS", "FAILED"}
RECOVERABLE_PART_STATUSES = {"PENDING", "PROCESSING", "FAILED"}


def now_utc():
    return datetime.now(timezone.utc)


def get_mongodb_collection():
    if not MONGODB_URI:
        raise ValueError("❌ MONGODB_URI environment variable is not set")
    try:
        print("🔌 Connecting to MongoDB Atlas...", flush=True)
        client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=MONGODB_SERVER_TIMEOUT_MS,
        )
        client.admin.command("ping")
        db = client[DATABASE_NAME]
        collection = db[COLLECTION_NAME]
        print("✅ MongoDB connection successful!", flush=True)
        print(f"📦 Database   : {DATABASE_NAME}", flush=True)
        print(f"📚 Collection : {COLLECTION_NAME}", flush=True)
        return client, collection
    except ConnectionFailure as exc:
        print(f"❌ MongoDB connection failed: {exc}", flush=True)
        raise


def _story_query(story):
    mongo_id = story.get("_id") if story else None
    if mongo_id is not None:
        return {"_id": mongo_id}
    story_id = story.get("story_id") or story.get("id") or story.get("ID") or "unknown"
    return {"$or": [{"story_id": story_id}, {"id": story_id}, {"ID": story_id}]}


def _story_id(story):
    return story.get("story_id") or story.get("id") or story.get("ID") or "unknown"


def update_story_status(story, status, extra_fields=None, retries=3):
    if not story:
        return False
    query = _story_query(story)
    story_id = _story_id(story)
    fields = {
        "status": status,
        "overall_status": status,
        "updated_at": now_utc(),
    }
    if extra_fields:
        fields.update(extra_fields)

    for attempt in range(1, retries + 1):
        client = None
        try:
            client, collection = get_mongodb_collection()
            result = collection.update_one(query, {"$set": fields})
            current = collection.find_one(query, {"status": 1})
            actual = current.get("status") if current else None
            if actual == status:
                print(
                    f"✅ MongoDB story status verified: {story_id} -> {status} "
                    f"(matched={result.matched_count}, modified={result.modified_count})",
                    flush=True,
                )
                return True
            print(
                f"⚠️ Story status verification {attempt}/{retries}: expected={status}, actual={actual}",
                flush=True,
            )
        except Exception as exc:
            print(f"⚠️ Story status update {attempt}/{retries} failed: {exc}", flush=True)
        finally:
            if client:
                client.close()
        if attempt < retries:
            time.sleep(2 * attempt)
    return False


def _normalize_parts(story):
    parts = story.get("parts")
    if isinstance(parts, list) and parts:
        normalized = []
        for index, part in enumerate(parts, start=1):
            if not isinstance(part, dict):
                continue
            try:
                part_no = int(part.get("part_no", index))
            except (TypeError, ValueError):
                part_no = index
            normalized.append({
                "part_no": part_no,
                "part_title": str(part.get("part_title") or f"Part {part_no}").strip(),
                "status": str(part.get("status") or "PENDING").upper(),
                "scenes": part.get("scenes") or [],
            })
        return sorted(normalized, key=lambda x: x["part_no"])

    scenes = story.get("scenes") or []
    if scenes:
        return [{"part_no": 1, "part_title": "Part 1", "status": "PENDING", "scenes": scenes}]
    return []


def _part_result(story, part_no):
    return (story.get("part_results") or {}).get(f"part_{int(part_no):02d}") or {}


def _platforms_posted(result):
    return (
        str(result.get("facebook_status") or "").upper() == "POSTED"
        and str(result.get("instagram_status") or "").upper() == "POSTED"
    )


def _part_needs_work(story, part):
    result = _part_result(story, part["part_no"])
    status = str(result.get("status") or part.get("status") or "PENDING").upper()
    if status == "SUCCESS" and _platforms_posted(result):
        return False
    return status in RECOVERABLE_PART_STATUSES or not _platforms_posted(result)


def _choose_part(story):
    """Choose the first unfinished part. Part and scene counts are unrestricted."""
    parts = _normalize_parts(story)
    if not parts:
        return None

    # First respect explicit PENDING work, then recover PROCESSING, then FAILED.
    priority = {"PENDING": 0, "PROCESSING": 1, "FAILED": 2}
    candidates = []
    for part in parts:
        result = _part_result(story, part["part_no"])
        status = str(result.get("status") or part.get("status") or "PENDING").upper()
        if _part_needs_work(story, part):
            candidates.append((priority.get(status, 3), part["part_no"], part))

    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates[0][2]


def _ensure_part_result_defaults(collection, story, part):
    """Create only missing part_result keys; never reset existing progress."""
    key = f"part_{int(part['part_no']):02d}"
    existing = (story.get("part_results") or {}).get(key)
    if existing:
        return
    collection.update_one(
        _story_query(story),
        {"$set": {
            f"part_results.{key}": {
                "part_no": int(part["part_no"]),
                "part_title": part.get("part_title", ""),
                "status": "PENDING",
                "error": None,
                "video_path": None,
                "facebook_status": "PENDING",
                "facebook_video_id": None,
                "instagram_status": "PENDING",
                "instagram_media_id": None,
                "started_at": None,
                "completed_at": None,
                "updated_at": now_utc(),
            }
        }},
    )


def update_part_status(story, part_no, status, extra_fields=None, retries=3):
    if not story:
        return False
    status = str(status).upper()
    if status not in PART_STATUSES:
        raise ValueError(f"Unsupported part status: {status}")

    query = _story_query(story)
    key = f"part_{int(part_no):02d}"
    path = f"part_results.{key}"
    fields = {
        f"{path}.part_no": int(part_no),
        f"{path}.status": status,
        f"{path}.updated_at": now_utc(),
    }
    if extra_fields:
        for field, value in extra_fields.items():
            fields[f"{path}.{field}"] = value

    # Keep the source part status synchronized too.
    fields["updated_at"] = now_utc()
    fields["status"] = "PROCESSING" if status in {"PENDING", "PROCESSING"} else None
    if status == "SUCCESS":
        fields.pop("status")

    for attempt in range(1, retries + 1):
        client = None
        try:
            client, collection = get_mongodb_collection()
            set_fields = dict(fields)
            # Only set top-level PROCESSING for unfinished work; do not force
            # top-level SUCCESS because another part may still be pending.
            if status in {"PENDING", "PROCESSING"}:
                set_fields["overall_status"] = "PROCESSING"
                set_fields["status"] = "PROCESSING"

            collection.update_one(query, {"$set": set_fields})
            current = collection.find_one(query, {f"part_results.{key}.status": 1})
            actual = ((current or {}).get("part_results") or {}).get(key, {}).get("status")
            if actual == status:
                print(f"✅ Part status verified: {_story_id(story)} / {key} -> {status}", flush=True)
                return True
        except Exception as exc:
            print(f"⚠️ Part status update {attempt}/{retries} failed: {exc}", flush=True)
        finally:
            if client:
                client.close()
        if attempt < retries:
            time.sleep(2 * attempt)
    return False


def get_story_from_mongodb():
    """Return exactly one story and exactly one part for this GitHub Actions run.

    Priority:
      1. Existing PROCESSING story with unfinished part.
      2. Oldest PENDING story.

    No fixed number of parts or scenes is enforced.
    """
    client, collection = get_mongodb_collection()
    try:
        # ------------------------------------------------------------
        # 1) Resume an existing PROCESSING story first.
        # ------------------------------------------------------------
        processing_filter = {
            "$or": [
                {"status": "PROCESSING"},
                {"overall_status": "PROCESSING"},
            ]
        }
        if STORY_ID:
            processing_filter = {
                "$and": [
                    {"story_id": STORY_ID},
                    processing_filter,
                ]
            }

        for story in collection.find(processing_filter).sort(
            [("processing_at", 1), ("updated_at", 1), ("story_no", 1), ("_id", 1)]
        ):
            part = _choose_part(story)
            if part is None:
                # Repair a story whose every part is fully posted.
                all_done = True
                for p in _normalize_parts(story):
                    if not _platforms_posted(_part_result(story, p["part_no"])):
                        all_done = False
                        break
                if all_done and _normalize_parts(story):
                    collection.update_one(
                        _story_query(story),
                        {"$set": {
                            "status": "COMPLETED",
                            "overall_status": "COMPLETED",
                            "completed_at": now_utc(),
                            "updated_at": now_utc(),
                            "last_error": None,
                        }},
                    )
                    print(f"✅ Repaired completed story: {_story_id(story)}", flush=True)
                continue

            _ensure_part_result_defaults(collection, story, part)
            print("==========================================", flush=True)
            print("♻️ RESUMING PROCESSING STORY", flush=True)
            print(f"🆔 Story ID   : {_story_id(story)}", flush=True)
            print(f"📖 Title      : {story.get('title') or 'Untitled Story'}", flush=True)
            print(f"🧩 Total parts: {len(_normalize_parts(story))}", flush=True)
            print(f"▶️ Part       : {part['part_no']}", flush=True)
            print(f"🎬 Scenes     : {len(part.get('scenes') or [])}", flush=True)
            print("🔄 Status     : PROCESSING", flush=True)
            print("==========================================", flush=True)
            return story, [part]

        # ------------------------------------------------------------
        # 2) No recoverable PROCESSING story: atomically claim PENDING.
        # ------------------------------------------------------------
        if STORY_ID:
            pending_query = {"story_id": STORY_ID, "status": "PENDING"}
        else:
            pending_query = {
                "$or": [
                    {"status": "PENDING"},
                    {"overall_status": "PENDING"},
                ]
            }

        story = collection.find_one_and_update(
            pending_query,
            {"$set": {
                "status": "PROCESSING",
                "overall_status": "PROCESSING",
                "processing_at": now_utc(),
                "updated_at": now_utc(),
                "last_error": None,
            }},
            sort=[("story_no", 1), ("created_at", 1), ("_id", 1)],
            return_document=ReturnDocument.AFTER,
        )

        if not story:
            print("ℹ️ No PROCESSING or PENDING story available.", flush=True)
            return None, []

        parts = _normalize_parts(story)
        if not parts:
            collection.update_one(
                _story_query(story),
                {"$set": {
                    "status": "PROCESSING",
                    "overall_status": "PROCESSING",
                    "last_error": "Story contains no parts.",
                    "updated_at": now_utc(),
                }},
            )
            print(f"⚠️ Story {_story_id(story)} contains no parts.", flush=True)
            return story, []

        part = _choose_part(story)
        if part is None:
            print(f"⚠️ Story {_story_id(story)} has no unfinished part.", flush=True)
            return story, []

        _ensure_part_result_defaults(collection, story, part)
        print("==========================================", flush=True)
        print("🚀 NEW STORY CLAIMED", flush=True)
        print(f"🆔 Story ID   : {_story_id(story)}", flush=True)
        print(f"📖 Title      : {story.get('title') or 'Untitled Story'}", flush=True)
        print(f"🧩 Total parts: {len(parts)}", flush=True)
        print(f"▶️ First part : {part['part_no']}", flush=True)
        print(f"🎬 Scenes     : {len(part.get('scenes') or [])}", flush=True)
        print("==========================================", flush=True)
        return story, [part]
    finally:
        client.close()
