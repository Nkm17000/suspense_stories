"""MongoDB connection and long-story / part-level status management."""

import time
from datetime import datetime, timezone

from pymongo import MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure

from .config import MONGODB_URI, STORY_ID

# Long-story collection requested for this pipeline.
DATABASE_NAME = "storydb"
COLLECTION_NAME = "longstory"

PART_STATUSES = {
    "PENDING",
    "PROCESSING",
    "SUCCESS",
    "FAILED",
}


def get_mongodb_collection():
    """Connect to the configured MongoDB database/longstory collection."""
    if not MONGODB_URI:
        raise ValueError("❌ MONGODB_URI environment variable is not set")

    try:
        print("🔌 Connecting to MongoDB Atlas...", flush=True)
        client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=10000,
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
                "scenes": part.get("scenes") or [],
            })

        return sorted(normalized, key=lambda item: item["part_no"])

    # Backwards-compatible fallback.
    scenes = story.get("scenes") or []
    if scenes:
        return [{
            "part_no": 1,
            "part_title": "Part 1",
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
            image_prompt = item.get("image_prompt")
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
    """Create top-level execution metadata without changing story content."""
    now = datetime.now(timezone.utc)
    part_results = {}
    for part in parts:
        key = f"part_{part['part_no']:02d}"
        part_results[key] = {
            "part_no": part["part_no"],
            "part_title": part["part_title"],
            "status": "PENDING",
            "error": None,
            "video_path": None,
            "facebook_status": "PENDING",
            "facebook_video_id": None,
            "started_at": None,
            "completed_at": None,
            "updated_at": now,
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
            collection.update_one(query, {"$set": fields})
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
    """Atomically claim one PENDING long story and return its validated parts."""
    client, collection = get_mongodb_collection()

    try:
        if STORY_ID:
            query = {"story_id": STORY_ID, "status": "PENDING"}
            sort_order = [("story_no", 1)]
            print(f"🔎 Requested STORY_ID: {STORY_ID}", flush=True)
        else:
            query = {"status": "PENDING"}
            sort_order = [("story_no", 1), ("story_id", 1)]
            print("🔎 Searching for next PENDING long story...", flush=True)

        story = collection.find_one_and_update(
            query,
            {
                "$set": {
                    "status": "PROCESSING",
                    "overall_status": "PROCESSING",
                    "processing_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                }
            },
            sort=sort_order,
            return_document=ReturnDocument.AFTER,
        )

        if not story:
            print("ℹ️ No PENDING long story available.", flush=True)
            return None, []

        story_id = (
            story.get("story_id")
            or story.get("id")
            or story.get("ID")
            or "unknown"
        )
        title = str(story.get("title") or "Untitled Story").strip()
        parts = _normalize_parts(story)

        if not parts:
            message = "Story contains no parts/scenes"
            collection.update_one(
                {"_id": story["_id"]},
                {"$set": {
                    "status": "FAILED",
                    "overall_status": "FAILED",
                    "last_error": message,
                    "updated_at": datetime.now(timezone.utc),
                }},
            )
            raise ValueError(f"❌ {message}")

        # Validate every part independently. Invalid parts remain in the list
        # with empty scenes so main.py can record FAILED and continue.
        validated_parts = []
        for part in parts:
            validated_parts.append({
                "part_no": part["part_no"],
                "part_title": part["part_title"],
                "scenes": _validate_part_scenes(part),
            })

        part_results = _initialize_part_results(story, validated_parts)
        collection.update_one(
            {"_id": story["_id"]},
            {"$set": {
                "part_results": part_results,
                "overall_status": "PROCESSING",
                "status": "PROCESSING",
                "total_parts": len(validated_parts),
                "updated_at": datetime.now(timezone.utc),
            }},
        )

        print("==========================================", flush=True)
        print("✅ LONG STORY CLAIMED", flush=True)
        print(f"🆔 Story ID   : {story_id}", flush=True)
        print(f"📖 Title      : {title}", flush=True)
        print(f"🧩 Total parts: {len(validated_parts)}", flush=True)
        print("🔄 Status     : PROCESSING", flush=True)
        print("==========================================", flush=True)

        for part in validated_parts:
            scene_count = len(part["scenes"])
            if scene_count != 10:
                print(
                    f"⚠️ Part {part['part_no']} has {scene_count} valid scenes; "
                    "expected 10. It will be processed independently.",
                    flush=True,
                )

        return story, validated_parts

    finally:
        client.close()
