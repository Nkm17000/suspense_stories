"""MongoDB connection, story claiming, and status management."""

from datetime import datetime, timezone
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure

from .config import MONGODB_URI, DATABASE_NAME, COLLECTION_NAME, STORY_ID

# ============================================================
# MONGODB CONNECTION
# ============================================================

def get_mongodb_collection():

    if not MONGODB_URI:
        raise ValueError(
            "❌ MONGODB_URI environment variable is not set"
        )

    try:

        print(
            "🔌 Connecting to MongoDB Atlas...",
            flush=True
        )

        client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=10000
        )

        # Test MongoDB connection
        client.admin.command("ping")

        print(
            "✅ MongoDB connection successful!",
            flush=True
        )

        db = client[DATABASE_NAME]

        collection = db[COLLECTION_NAME]

        print(
            f"📦 Database: {DATABASE_NAME}",
            flush=True
        )

        print(
            f"📚 Collection: {COLLECTION_NAME}",
            flush=True
        )

        return client, collection

    except ConnectionFailure as e:

        print(
            "❌ MongoDB connection failed:",
            e,
            flush=True
        )

        raise


# ============================================================
# MONGODB STATUS HELPERS
# ============================================================

def update_story_status(story, status, extra_fields=None, retries=3):
    """
    Reliably update and VERIFY the MongoDB status.

    The primary key is the document's _id. This avoids failures caused
    by a missing/different story_id field name. story_id/id/ID are also
    supported for compatibility with existing documents.
    """
    if not story:
        print("⚠️ Cannot update MongoDB status: story document is missing", flush=True)
        return False

    mongo_id = story.get("_id")
    story_id = (
        story.get("story_id")
        or story.get("id")
        or story.get("ID")
        or "unknown"
    )

    fields = {
        "status": status,
        "updated_at": datetime.now(timezone.utc)
    }

    if extra_fields:
        fields.update(extra_fields)

    if status == "COMPLETED":
        fields["story_id"] = story_id

    for attempt in range(1, retries + 1):
        client = None
        try:
            client, collection = get_mongodb_collection()

            if mongo_id is not None:
                query = {"_id": mongo_id}
            else:
                # Compatibility fallback for documents without _id in
                # an unusual test/mock collection.
                query = {
                    "$or": [
                        {"story_id": story_id},
                        {"id": story_id},
                        {"ID": story_id}
                    ]
                }

            result = collection.update_one(
                query,
                {"$set": fields}
            )

            current = collection.find_one(
                query,
                {"status": 1, "story_id": 1, "id": 1, "ID": 1}
            )

            actual_status = current.get("status") if current else None

            if actual_status == status:
                print(
                    f"✅ MongoDB status verified: {story_id} -> {status} "
                    f"(matched={result.matched_count}, "
                    f"modified={result.modified_count})",
                    flush=True
                )
                return True

            print(
                f"⚠️ MongoDB verification attempt "
                f"{attempt}/{retries}: expected={status}, "
                f"actual={actual_status}, story={story_id}",
                flush=True
            )

        except Exception as e:
            print(
                f"⚠️ MongoDB status update attempt "
                f"{attempt}/{retries} failed: {e}",
                flush=True
            )
        finally:
            if client:
                client.close()

        if attempt < retries:
            time.sleep(2 * attempt)

    return False


# ============================================================
# GET STORY FROM MONGODB
# ============================================================

def get_story_from_mongodb():
    """Atomically claim exactly one PENDING story."""
    client, collection = get_mongodb_collection()

    try:
        if STORY_ID:
            print(f"🔎 Requested STORY_ID: {STORY_ID}", flush=True)
            query = {"story_id": STORY_ID, "status": "PENDING"}
        else:
            print("🔎 STORY_ID not provided.", flush=True)
            print("📖 Searching for next PENDING story...", flush=True)
            query = {"status": "PENDING"}

        story = collection.find_one_and_update(
            query,
            {"$set": {"status": "PROCESSING"}},
            sort=[("story_no", 1), ("story_id", 1)],
            return_document=ReturnDocument.AFTER
        )

        if not story:
            print("ℹ️ No PENDING story available.", flush=True)
            return None, []

        title = story.get("title", "Untitled Story")
        story_id = (story.get("story_id") or story.get("id") or story.get("ID") or "unknown")

        print("✅ Story claimed successfully", flush=True)
        print(f"🆔 Story ID: {story_id}", flush=True)
        print(f"📖 Title: {title}", flush=True)
        print("🔄 Status: PROCESSING", flush=True)

        scenes = story.get("scenes", [])

        if not scenes:
            collection.update_one(
                {"_id": story["_id"], "status": "PROCESSING"},
                {"$set": {"status": "FAILED", "last_error": "Story contains no scenes"}}
            )
            raise ValueError("❌ Story contains no scenes")

        print(f"🎬 Total scenes: {len(scenes)}", flush=True)

        valid_scenes = []

        for scene in scenes:
            text = scene.get("text")
            sub_image_prompts = scene.get("sub_image_prompts")

            if not text:
                print("⚠️ Scene skipped: missing text", flush=True)
                continue

            if not isinstance(sub_image_prompts, list) or not sub_image_prompts:
                print("⚠️ Scene skipped: missing sub_image_prompts array", flush=True)
                continue

            valid_sub_prompts = []

            for sub_index, item in enumerate(sub_image_prompts, start=1):
                if not isinstance(item, dict):
                    print(
                        f"⚠️ Scene {scene.get('scene_number', '?')}: "
                        f"sub_image_prompts item {sub_index} is not an object",
                        flush=True
                    )
                    continue

                sub_text = item.get("text")
                image_prompt = item.get("image_prompt")

                if not image_prompt or not isinstance(image_prompt, str):
                    print(
                        f"⚠️ Scene {scene.get('scene_number', '?')}: "
                        f"sub-image {sub_index} missing image_prompt",
                        flush=True
                    )
                    continue

                valid_sub_prompts.append({
                    "text": sub_text.strip() if isinstance(sub_text, str) else "",
                    "image_prompt": image_prompt.strip()
                })

            if not valid_sub_prompts:
                print(
                    f"⚠️ Scene {scene.get('scene_number', '?')}: "
                    "no valid sub-image prompts",
                    flush=True
                )
                continue

            valid_scenes.append({
                "scene_number": scene.get(
                    "scene_number",
                    len(valid_scenes) + 1
                ),
                "text": text,
                "sub_image_prompts": valid_sub_prompts
            })

        if not valid_scenes:
            collection.update_one(
                {"_id": story["_id"], "status": "PROCESSING"},
                {"$set": {"status": "FAILED", "last_error": "No valid scenes found"}}
            )
            raise ValueError("❌ No valid scenes found")

        print(f"✅ Valid scenes: {len(valid_scenes)}", flush=True)
        return story, valid_scenes

    finally:
        client.close()
