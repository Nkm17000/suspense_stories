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
    "PARTIAL_COMPLETED",
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


def _coerce_list(value):
    """Convert common MongoDB/JSON representations into a Python list."""
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        try:
            import json
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _extract_nested_scenes(part):
    """Read scenes from several compatible part layouts."""
    candidates = [
        part.get("scenes"),
        part.get("scene_list"),
        part.get("scene_data"),
    ]

    for container_key in ("content", "data", "story"):
        container = part.get(container_key)
        if isinstance(container, dict):
            candidates.extend([
                container.get("scenes"),
                container.get("scene_list"),
                container.get("scene_data"),
            ])

    for candidate in candidates:
        scenes = _coerce_list(candidate)
        if scenes:
            return scenes, "part-nested"

    return [], "missing"


def _extract_top_level_scenes_for_part(story, part, part_index, part_count):
    """Fallback for long-story documents that store scenes at story.scenes.

    Preferred mapping uses explicit scene_start/scene_end metadata. If that is
    absent, an even contiguous split is used only when the scene count divides
    evenly across parts. This avoids silently assigning arbitrary scenes.
    """
    top_level = _coerce_list(story.get("scenes"))
    if not top_level:
        return [], "missing"

    start_keys = ("scene_start", "start_scene", "first_scene")
    end_keys = ("scene_end", "end_scene", "last_scene")
    start = next((part.get(k) for k in start_keys if part.get(k) is not None), None)
    end = next((part.get(k) for k in end_keys if part.get(k) is not None), None)

    if start is not None or end is not None:
        try:
            start_i = max(1, int(start or 1))
            end_i = int(end or len(top_level))
            selected = [
                scene for scene in top_level
                if isinstance(scene, dict)
                and start_i <= int(scene.get("scene_number", 0) or 0) <= end_i
            ]
            if selected:
                return selected, f"top-level-range-{start_i}-{end_i}"
        except (TypeError, ValueError):
            pass

    if part_count > 0 and len(top_level) % part_count == 0:
        chunk = len(top_level) // part_count
        begin = part_index * chunk
        end = begin + chunk
        return top_level[begin:end], f"top-level-even-split-{begin + 1}-{end}"

    return [], "top-level-present-but-no-safe-mapping"


def _normalize_parts(story):
    """Return long-story parts while preserving enough source data to diagnose errors."""
    parts = story.get("parts")

    if isinstance(parts, list) and parts:
        normalized = []
        part_count = len(parts)
        for index, raw_part in enumerate(parts, start=1):
            if not isinstance(raw_part, dict):
                normalized.append({
                    "part_no": index,
                    "part_title": f"Part {index}",
                    "status": "PENDING",
                    "scenes": [],
                    "_scene_source": "invalid-part-object",
                    "_raw_scene_keys": [],
                })
                continue

            part_no = raw_part.get("part_no", raw_part.get("part_number", index))
            try:
                part_no = int(part_no)
            except (TypeError, ValueError):
                part_no = index

            scenes, source = _extract_nested_scenes(raw_part)
            if not scenes:
                scenes, source = _extract_top_level_scenes_for_part(
                    story, raw_part, index - 1, part_count
                )

            normalized.append({
                "part_no": part_no,
                "part_title": str(
                    raw_part.get("part_title")
                    or raw_part.get("title")
                    or f"Part {part_no}"
                ).strip(),
                # NULL, missing, or blank status is an actionable PENDING part.
                "status": str(raw_part.get("status") or "PENDING").strip().upper(),
                "scenes": scenes,
                "_scene_source": source,
                "_raw_scene_keys": [
                    k for k in raw_part.keys()
                    if "scene" in str(k).lower()
                ],
            })

        return sorted(normalized, key=lambda item: item["part_no"])

    scenes = _coerce_list(story.get("scenes"))
    if scenes:
        return [{
            "part_no": 1,
            "part_title": "Part 1",
            "status": "PENDING",
            "scenes": scenes,
            "_scene_source": "legacy-top-level",
            "_raw_scene_keys": ["scenes"],
        }]

    return []


def _validate_part_scenes(part):
    """Validate scenes while returning useful diagnostics instead of silently dropping data."""
    valid_scenes = []
    scenes = _coerce_list(part.get("scenes"))
    invalid_reasons = []

    for scene_index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict):
            invalid_reasons.append(f"scene[{scene_index}] is not an object")
            continue

        text = scene.get("text") or scene.get("scene_text") or scene.get("narration")
        prompts = (
            scene.get("sub_image_prompts")
            or scene.get("sub_image_prompt")
            or scene.get("image_prompts")
        )

        if not text:
            invalid_reasons.append(f"scene[{scene_index}] missing text/scene_text/narration")
            continue
        prompts = _coerce_list(prompts)
        if not prompts:
            invalid_reasons.append(f"scene[{scene_index}] has no image prompts")
            continue

        valid_prompts = []
        for prompt_index, item in enumerate(prompts, start=1):
            if not isinstance(item, dict):
                invalid_reasons.append(
                    f"scene[{scene_index}] prompt[{prompt_index}] is not an object"
                )
                continue
            image_prompt = item.get("scene_prompt") or item.get("image_prompt") or item.get("prompt")
            if not isinstance(image_prompt, str) or not image_prompt.strip():
                invalid_reasons.append(
                    f"scene[{scene_index}] prompt[{prompt_index}] missing scene_prompt/image_prompt/prompt"
                )
                continue

            valid_prompts.append({
                "text": str(item.get("text") or item.get("sub_text") or "").strip(),
                "image_prompt": image_prompt.strip(),
            })

        if not valid_prompts:
            continue

        scene_number = scene.get("scene_number", scene.get("scene_no", len(valid_scenes) + 1))
        try:
            scene_number = int(scene_number)
        except (TypeError, ValueError):
            scene_number = len(valid_scenes) + 1

        valid_scenes.append({
            "scene_number": scene_number,
            "text": str(text).strip(),
            "sub_image_prompts": valid_prompts,
        })

    return valid_scenes, invalid_reasons

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
    """Claim/resume a long suspense story according to the part state machine.

    Rules:
      1. Existing PROCESSING story with any NULL/missing/blank/PENDING part:
         resume that story first and process its FIRST actionable part only.
      2. If no resumable PROCESSING story exists, claim a top-level PENDING story.
         A newly claimed PENDING story is a fresh run: every part is reset to
         PENDING and ALL parts are returned for processing, including parts that
         were previously SUCCESS/FAILED. This intentionally restarts the story.
      3. NULL/missing/blank part status is always treated as PENDING.
      4. PROCESSING + no actionable parts + one or more FAILED parts + all other
         parts terminal => PARTIAL_COMPLETED is the final story state.
    """
    client, collection = get_mongodb_collection()
    try:
        now = datetime.now(timezone.utc)
        story = None
        resumed_processing = False

        if STORY_ID:
            base_query = {"story_id": STORY_ID}
        else:
            base_query = {}

        # ------------------------------------------------------------
        # 1) ALWAYS RESUME AN EXISTING PROCESSING STORY FIRST.
        # ------------------------------------------------------------
        processing_query = dict(base_query)
        processing_query["status"] = "PROCESSING"
        candidates = collection.find(processing_query).sort(
            [("processing_at", 1), ("story_no", 1), ("story_id", 1)]
        )

        for candidate in candidates:
            raw_parts = candidate.get("parts")
            if not isinstance(raw_parts, list):
                continue

            actionable = [
                p for p in raw_parts
                if isinstance(p, dict)
                and str(p.get("status") or "PENDING").strip().upper() == "PENDING"
            ]
            if actionable:
                story = candidate
                resumed_processing = True
                break

        # ------------------------------------------------------------
        # 2) IF NOTHING IS PROCESSING, CLAIM A NEW PENDING STORY.
        # ------------------------------------------------------------
        if not story:
            pending_query = dict(base_query)
            pending_query["status"] = {"$in": ["PENDING", None, ""]}
            story = collection.find_one_and_update(
                pending_query,
                {
                    "$set": {
                        "status": "PROCESSING",
                        "overall_status": "PROCESSING",
                        "processing_at": now,
                        "updated_at": now,
                    }
                },
                sort=[("story_no", 1), ("story_id", 1)],
                return_document=ReturnDocument.AFTER,
            )

        if not story:
            print(
                "ℹ️ No PROCESSING story with actionable parts and no new PENDING story available.",
                flush=True,
            )
            return None, []

        story_id = story.get("story_id") or story.get("id") or story.get("ID") or "unknown"
        title = str(story.get("title") or "Untitled Story").strip()
        original_story_status = "PROCESSING" if resumed_processing else "PENDING"

        # ------------------------------------------------------------
        # DIAGNOSTIC STRUCTURE LOGGING
        # ------------------------------------------------------------
        raw_parts = story.get("parts")
        print(f"🔎 Mongo story top-level keys: {sorted(str(k) for k in story.keys())}", flush=True)
        print(
            f"🔎 Mongo story status at selection: {original_story_status} -> PROCESSING",
            flush=True,
        )
        print(
            f"🔎 Mongo parts type/count: {type(raw_parts).__name__}/{len(raw_parts) if isinstance(raw_parts, list) else 0}",
            flush=True,
        )
        if isinstance(raw_parts, list):
            for raw_index, raw_part in enumerate(raw_parts[:50], start=1):
                if isinstance(raw_part, dict):
                    scene_keys = [k for k in raw_part.keys() if "scene" in str(k).lower()]
                    raw_status = raw_part.get("status")
                    print(
                        f"🔎 Raw part {raw_index}: status={raw_status!r}; "
                        f"keys={sorted(str(k) for k in raw_part.keys())}; "
                        f"scene-related-keys={scene_keys}; "
                        f"scenes_type={type(raw_part.get('scenes')).__name__}; "
                        f"scenes_count={len(raw_part.get('scenes') or []) if isinstance(raw_part.get('scenes'), list) else 0}",
                        flush=True,
                    )
                else:
                    print(f"🔎 Raw part {raw_index}: type={type(raw_part).__name__}", flush=True)

        top_scenes = story.get("scenes")
        print(
            f"🔎 Top-level scenes type/count: {type(top_scenes).__name__}/"
            f"{len(top_scenes) if isinstance(top_scenes, list) else 0}",
            flush=True,
        )

        all_parts = _normalize_parts(story)

        # ------------------------------------------------------------
        # NEW PENDING STORY = FULL RESET / FULL PROCESS
        # ------------------------------------------------------------
        if not resumed_processing:
            print(
                "🆕 NEW PENDING STORY: resetting EVERY part to PENDING and "
                "processing ALL parts in this run.",
                flush=True,
            )
            for part in all_parts:
                part["status"] = "PENDING"

        validated_all = []
        for part in all_parts:
            valid_scenes, validation_errors = _validate_part_scenes(part)
            validated_all.append({
                "part_no": part["part_no"],
                "part_title": part["part_title"],
                "status": "PENDING" if not resumed_processing else str(
                    part.get("status") or "PENDING"
                ).strip().upper(),
                "scenes": valid_scenes,
                "_validation_errors": validation_errors,
                "_scene_source": part.get("_scene_source", "unknown"),
                "_raw_scene_keys": part.get("_raw_scene_keys", []),
            })

        part_results = _initialize_part_results(story, validated_all)
        collection.update_one(
            {"_id": story["_id"]},
            {
                "$set": {
                    "part_results": part_results,
                    "overall_status": "PROCESSING",
                    "status": "PROCESSING",
                    "total_parts": len(validated_all),
                    "updated_at": now,
                }
            },
        )

        # For a resumed PROCESSING story, process ONLY the first actionable
        # part. For a newly PENDING story, process ALL parts.
        if resumed_processing:
            actionable = [
                p for p in validated_all
                if str(p.get("status") or "PENDING").strip().upper() == "PENDING"
            ]
            selected_parts = actionable[:1]
        else:
            selected_parts = validated_all

        story["part_results"] = part_results
        story["parts"] = validated_all
        story["_resumed_processing"] = resumed_processing
        story["_original_status"] = original_story_status

        print("==========================================", flush=True)
        print("✅ LONG STORY CLAIMED/RESUMED", flush=True)
        print(f"🆔 Story ID   : {story_id}", flush=True)
        print(f"📖 Title      : {title}", flush=True)
        print(f"🧩 Total parts: {len(validated_all)}", flush=True)
        print(
            "▶️ Actionable parts (NULL/missing/PENDING): "
            f"{[p['part_no'] for p in validated_all if str(p.get('status') or 'PENDING').strip().upper() == 'PENDING']}",
            flush=True,
        )
        print(
            f"▶️ Parts selected for THIS RUN: {[p['part_no'] for p in selected_parts]}",
            flush=True,
        )
        print(
            "🔁 Mode: " + ("RESUME PROCESSING (first actionable part)" if resumed_processing else "NEW PENDING (all parts)"),
            flush=True,
        )
        print("🔄 Status     : PROCESSING", flush=True)
        print("==========================================", flush=True)

        for part in selected_parts:
            if not part["scenes"]:
                print(f"❌ Part {part['part_no']} has 0 valid scenes.", flush=True)
                print(f"   Scene source detected : {part.get('_scene_source')}", flush=True)
                print(f"   Raw scene-related keys: {part.get('_raw_scene_keys')}", flush=True)
                errors = part.get("_validation_errors") or []
                if errors:
                    for reason in errors[:10]:
                        print(f"   • {reason}", flush=True)
                else:
                    print(
                        "   • No scenes were found in the part or a safely mappable top-level story.scenes.",
                        flush=True,
                    )
            else:
                print(
                    f"✅ Part {part['part_no']}: {len(part['scenes'])} valid scenes "
                    f"(source={part.get('_scene_source')})",
                    flush=True,
                )

        return story, selected_parts
    finally:
        client.close()
