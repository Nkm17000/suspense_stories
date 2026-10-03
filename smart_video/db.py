"""MongoDB connection and long-story / part-level status management."""

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

# Long-story collection requested for this pipeline.

PART_STATUSES = {
    "PENDING",
    "PROCESSING",
    "SUCCESS",
    "FAILED",
    "SKIPPED",
}


def get_mongodb_collection():
    """Connect to the configured MongoDB database/longstory collection."""
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
    """Build the safest query for a story document."""
    mongo_id = story.get("_id") if story else None
    if mongo_id is not None:
        return {"_id": mongo_id}

    story_id = (
        story.get("story_id")
        or story.get("id")
        or story.get("ID")
        or "unknown"
    )
    return {
        "$or": [
            {"story_id": story_id},
            {"id": story_id},
            {"ID": story_id},
        ]
    }


def update_story_status(story, status, extra_fields=None, retries=3):
    """Update and verify the top-level story status."""
    if not story:
        print("⚠️ Cannot update story status: story is missing", flush=True)
        return False

    query = _story_query(story)
    story_id = (
        story.get("story_id")
        or story.get("id")
        or story.get("ID")
        or "unknown"
    )

    fields = {
        "status": status,
        "overall_status": status,
        "updated_at": datetime.now(timezone.utc),
    }
    if extra_fields:
        fields.update(extra_fields)

    for attempt in range(1, retries + 1):
        client = None
        try:
            client, collection = get_mongodb_collection()
            result = collection.update_one(query, {"$set": fields})
            current = collection.find_one(
                query,
                {"status": 1, "overall_status": 1, "story_id": 1},
            )
            actual = current.get("status") if current else None

            if actual == status:
                print(
                    f"✅ MongoDB story status verified: {story_id} -> {status} "
                    f"(matched={result.matched_count}, modified={result.modified_count})",
                    flush=True,
                )
                return True

            print(
                f"⚠️ Story status verification {attempt}/{retries}: "
                f"expected={status}, actual={actual}",
                flush=True,
            )

        except Exception as exc:
            print(
                f"⚠️ Story status update {attempt}/{retries} failed: {exc}",
                flush=True,
            )
        finally:
            if client:
                client.close()

        if attempt < retries:
            time.sleep(2 * attempt)

    return False


def _normalize_parts(story):
    """Return the long-story parts in MongoDB order.

    Expected schema:
        parts: [
            {"part_no": 1, "part_title": "...", "scenes": [...]},
            ...
        ]

    A legacy single-scenes story is accepted as Part 1 so the pipeline
    remains backwards compatible, but long-story documents should use parts.
    """
    parts = story.get("parts")

    if isinstance(parts, list) and parts:
        normalized = []
        for index, part in enumerate(parts, start=1):
            if not isinstance(part, dict):
                normalized.append({
                    "part_no": index,
                    "part_title": f"Part {index}",
                    "scenes": [],
                })
                continue

            part_no = part.get("part_no", index)
            try:
                part_no = int(part_no)
            except (TypeError, ValueError):
                part_no = index

            normalized.append({
                "part_no": part_no,
                "part_title": str(part.get("part_title") or f"Part {part_no}").strip(),
                "status": str(part.get("status") or "PENDING").upper(),
                "scenes": part.get("scenes") or [],
            })

        return sorted(normalized, key=lambda item: item["part_no"])

    # Backwards-compatible fallback.
    scenes = story.get("scenes") or []
    if scenes:
        return [{
            "part_no": 1,
            "part_title": "Part 1",
            "status": "PENDING",
            "scenes": scenes,
        }]

    return []


def _validate_part_scenes(part):
    """Validate one part without allowing one bad part to stop the story."""
    valid_scenes = []
    scenes = part.get("scenes") or []

    for scene in scenes:
        if not isinstance(scene, dict):
            continue

        text = scene.get("text")
        prompts = scene.get("sub_image_prompts")

        if not text:
            continue
        if not isinstance(prompts, list) or not prompts:
            continue

        valid_prompts = []
        for item in prompts:
            if not isinstance(item, dict):
                continue
            image_prompt = item.get("scene_prompt") or item.get("image_prompt")
            if not isinstance(image_prompt, str) or not image_prompt.strip():
                continue

            valid_prompts.append({
                "text": str(item.get("text") or "").strip(),
                "image_prompt": image_prompt.strip(),
            })

        if not valid_prompts:
            continue

        scene_number = scene.get("scene_number", len(valid_scenes) + 1)
        try:
            scene_number = int(scene_number)
        except (TypeError, ValueError):
            scene_number = len(valid_scenes) + 1

        valid_scenes.append({
            "scene_number": scene_number,
            "text": str(text).strip(),
            "sub_image_prompts": valid_prompts,
        })

    return valid_scenes


def _initialize_part_results(story, parts):
    """Create/preserve part execution metadata; never reset completed/skipped parts."""
    now = datetime.now(timezone.utc)
    existing = story.get("part_results") or {}
    part_results = {}
    for part in parts:
        key = f"part_{part['part_no']:02d}"
        old = existing.get(key, {}) if isinstance(existing, dict) else {}
        status = str(part.get("status") or old.get("status") or "PENDING").upper()
        if status not in PART_STATUSES:
            status = "PENDING"
        part_results[key] = {
            "part_no": part["part_no"],
            "part_title": part["part_title"],
            "status": status,
            "error": old.get("error"),
            "video_path": old.get("video_path"),
            "facebook_status": old.get("facebook_status", "PENDING"),
            "facebook_video_id": old.get("facebook_video_id"),
            "instagram_status": old.get("instagram_status", "PENDING"),
            "instagram_media_id": old.get("instagram_media_id"),
            "started_at": old.get("started_at"),
            "completed_at": old.get("completed_at"),
            "updated_at": now,
            "cloudflare_neurons_used": float(old.get("cloudflare_neurons_used", 0.0) or 0.0),
            "estimated_cloudflare_neurons": float(old.get("estimated_cloudflare_neurons", 0.0) or 0.0),
            "reported_cloudflare_neurons": float(old.get("reported_cloudflare_neurons", 0.0) or 0.0),
        }
    return part_results

def update_part_status(story, part_no, status, extra_fields=None, retries=3):
    """Update one part result under part_results.part_XX."""
    if not story:
        return False
    if status not in PART_STATUSES:
        raise ValueError(f"Unsupported part status: {status}")

    query = _story_query(story)
    key = f"part_{int(part_no):02d}"
    path = f"part_results.{key}"
    fields = {
        f"{path}.part_no": int(part_no),
        f"{path}.status": status,
        f"parts.$[p].status": status,
        f"{path}.updated_at": datetime.now(timezone.utc),
    }
    if extra_fields:
        for field, value in extra_fields.items():
            fields[f"{path}.{field}"] = value

    story_id = (
        story.get("story_id")
        or story.get("id")
        or story.get("ID")
        or "unknown"
    )

    for attempt in range(1, retries + 1):
        client = None
        try:
            client, collection = get_mongodb_collection()
            collection.update_one(query, {"$set": fields}, array_filters=[{"p.part_no": int(part_no)}])
            current = collection.find_one(
                query,
                {f"{path}.status": 1},
            )
            current_status = (
                current.get("part_results", {})
                .get(key, {})
                .get("status")
                if current else None
            )
            if current_status == status:
                print(
                    f"✅ Part status verified: {story_id} / {key} -> {status}",
                    flush=True,
                )
                return True

            print(
                f"⚠️ Part status verification {attempt}/{retries}: "
                f"expected={status}, actual={current_status}",
                flush=True,
            )

        except Exception as exc:
            print(
                f"⚠️ Part status update {attempt}/{retries} failed: {exc}",
                flush=True,
            )
        finally:
            if client:
                client.close()

        if attempt < retries:
            time.sleep(2 * attempt)

    return False


def get_story_from_mongodb():
    """Claim one story and return ONLY its PENDING parts.

    A story may be newly PENDING or already PROCESSING from an earlier run.
    Completed/skipped/failed parts are not rebuilt unless their part status is
    manually changed back to PENDING.
    """
    client, collection = get_mongodb_collection()
    try:
        story = None
        now = datetime.now(timezone.utc)
        if STORY_ID:
            base_query = {"story_id": STORY_ID, "status": {"$in": ["PENDING", "PROCESSING", "FAILED", "PARTIAL_SUCCESS"]}}
        else:
            base_query = {"status": {"$in": ["PENDING", "PROCESSING", "FAILED", "PARTIAL_SUCCESS"]}}

        # First claim a new PENDING story.
        query = dict(base_query)
        query["status"] = "PENDING"
        story = collection.find_one_and_update(
            query,
            {"$set": {"status": "PROCESSING", "overall_status": "PROCESSING", "processing_at": now, "updated_at": now}},
            sort=[("story_no", 1), ("story_id", 1)],
            return_document=ReturnDocument.AFTER,
        )

        # If no new story exists, resume an existing PROCESSING story only if it has PENDING parts.
        if not story:
            query = dict(base_query)
            query["status"] = "PROCESSING"
            query["$or"] = [
                {"parts": {"$elemMatch": {"status": "PENDING"}}},
                {"part_results": {"$exists": True}},
            ]
            candidates = collection.find(query).sort([("story_no", 1), ("story_id", 1)])
            for candidate in candidates:
                parts_raw = candidate.get("parts") or []
                pending_exists = any(str(p.get("status", "PENDING")).upper() == "PENDING" for p in parts_raw if isinstance(p, dict))
                if pending_exists:
                    story = candidate
                    break

        if not story:
            print("ℹ️ No PENDING story or PROCESSING story with PENDING parts available.", flush=True)
            return None, []

        story_id = story.get("story_id") or story.get("id") or story.get("ID") or "unknown"
        title = str(story.get("title") or "Untitled Story").strip()
        all_parts = _normalize_parts(story)
        validated_all = []
        for part in all_parts:
            validated_all.append({
                "part_no": part["part_no"],
                "part_title": part["part_title"],
                "status": str(part.get("status") or "PENDING").upper(),
                "scenes": _validate_part_scenes(part),
            })

        part_results = _initialize_part_results(story, validated_all)
        collection.update_one(
            {"_id": story["_id"]},
            {"$set": {"part_results": part_results, "overall_status": "PROCESSING", "status": "PROCESSING", "total_parts": len(validated_all), "updated_at": now}},
        )

        # Only return PENDING parts to the video pipeline.
        pending_parts = [p for p in validated_all if p["status"] == "PENDING"]
        story["part_results"] = part_results
        story["parts"] = validated_all

        print("==========================================", flush=True)
        print("✅ LONG STORY CLAIMED/RESUMED", flush=True)
        print(f"🆔 Story ID   : {story_id}", flush=True)
        print(f"📖 Title      : {title}", flush=True)
        print(f"🧩 Total parts: {len(validated_all)}", flush=True)
        print(f"▶️ Pending parts to execute: {[p['part_no'] for p in pending_parts]}", flush=True)
        print("🔄 Status     : PROCESSING", flush=True)
        print("==========================================", flush=True)

        for part in pending_parts:
            if not part["scenes"]:
                print(
                    f"⚠️ Part {part['part_no']} has no scenes and will fail validation.",
                    flush=True,
                )

        return story, pending_parts
    finally:
        client.close()

