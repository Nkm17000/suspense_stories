"""Sequential long-story orchestrator: build and publish one part at a time."""

import os
import sys
from datetime import datetime, timezone

from smart_video.db import (
    get_story_from_mongodb,
    update_part_status,
    update_story_status,
)
from smart_video.video_builder import build_part_video
from facebook_upload import upload_video


def export_story_to_github_actions(story_id, title):
    """Export story metadata for debugging/artifact steps."""
    github_env = os.getenv("GITHUB_ENV")
    if not github_env:
        return

    delimiter = "STORY_VALUE_DELIMITER_9f3a7c"
    with open(github_env, "a", encoding="utf-8") as env_file:
        env_file.write(f"STORY_ID<<{delimiter}\n{story_id}\n{delimiter}\n")
        env_file.write(f"STORY_TITLE<<{delimiter}\n{title}\n{delimiter}\n")


def _part_label(part_no):
    return f"भाग {part_no}"


def _mark_part_failed(story, part_no, error, video_path=None, facebook_status="FAILED"):
    update_part_status(
        story,
        part_no,
        "FAILED",
        {
            "error": str(error),
            "video_path": video_path,
            "facebook_status": facebook_status,
            "failed_at": datetime.now(timezone.utc),
        },
    )


def main():
    print("🚀 Starting SmartStudyLab LONG STORY generator...", flush=True)
    story = None
    successful_parts = 0
    failed_parts = 0

    try:
        story, parts = get_story_from_mongodb()

        if not story:
            print("ℹ️ No PENDING long story was found.", flush=True)
            return 2

        story_id = (
            story.get("story_id")
            or story.get("id")
            or story.get("ID")
            or "unknown"
        )
        title = str(story.get("title") or "Untitled Story").strip()

        print("==========================================", flush=True)
        print("📚 LONG STORY PROCESSING", flush=True)
        print(f"🆔 Story ID : {story_id}", flush=True)
        print(f"📖 Title    : {title}", flush=True)
        print(f"🧩 Parts    : {len(parts)}", flush=True)
        print("==========================================", flush=True)

        export_story_to_github_actions(story_id, title)

        # STRICTLY SEQUENTIAL: Part 1 completes (including Facebook upload)
        # before Part 2 starts.
        for part in parts:
            part_no = int(part["part_no"])
            part_title = str(part.get("part_title") or _part_label(part_no)).strip()
            scenes = part.get("scenes") or []

            print("\n==========================================", flush=True)
            print(f"▶️ START PART {part_no}", flush=True)
            print(f"📖 {title}", flush=True)
            print(f"🏷️ {part_title}", flush=True)
            print(f"🎬 Scenes: {len(scenes)}/10", flush=True)
            print("==========================================", flush=True)

            update_part_status(
                story,
                part_no,
                "PROCESSING",
                {
                    "error": None,
                    "started_at": datetime.now(timezone.utc),
                    "facebook_status": "PROCESSING",
                },
            )

            if len(scenes) != 10:
                error = (
                    f"Part {part_no} must contain exactly 10 valid scenes; "
                    f"found {len(scenes)}"
                )
                print(f"❌ {error}", flush=True)
                _mark_part_failed(story, part_no, error)
                failed_parts += 1
                continue

            video_path = None

            try:
                # ----------------------------------------------
                # 1. Build this part only.
                # ----------------------------------------------
                video_path = build_part_video(
                    scenes=scenes,
                    title=title,
                    part_no=part_no,
                    part_title=part_title,
                )

                # ----------------------------------------------
                # 2. Publish THIS part before starting next part.
                # ----------------------------------------------
                print(
                    f"\n📣 Publishing Part {part_no} to Facebook...",
                    flush=True,
                )

                facebook_video_id = upload_video(
                    video_path=video_path,
                    story_title=title,
                    part_no=part_no,
                    part_title=part_title,
                    story_id=story_id,
                )

                update_part_status(
                    story,
                    part_no,
                    "SUCCESS",
                    {
                        "video_path": video_path,
                        "facebook_status": "POSTED",
                        "facebook_video_id": facebook_video_id,
                        "error": None,
                        "completed_at": datetime.now(timezone.utc),
                    },
                )

                successful_parts += 1
                print(
                    f"✅ PART {part_no} SUCCESS - video created and posted",
                    flush=True,
                )

            except Exception as exc:
                failed_parts += 1
                print(
                    f"❌ PART {part_no} FAILED: {exc}",
                    flush=True,
                )
                _mark_part_failed(
                    story,
                    part_no,
                    exc,
                    video_path=video_path,
                    facebook_status=(
                        "FAILED" if video_path is None else "UPLOAD_FAILED"
                    ),
                )
                print(
                    f"➡️ Continuing with Part {part_no + 1}...",
                    flush=True,
                )

        # ------------------------------------------------------
        # Overall result is stored at the top level.
        # ------------------------------------------------------
        if failed_parts == 0 and successful_parts > 0:
            overall = "SUCCESS"
        elif successful_parts > 0:
            overall = "PARTIAL_SUCCESS"
        else:
            overall = "FAILED"

        summary = {
            "total_parts": len(parts),
            "successful_parts": successful_parts,
            "failed_parts": failed_parts,
            "completed_at": datetime.now(timezone.utc),
            "last_error": (
                None
                if failed_parts == 0
                else f"{failed_parts} part(s) failed. See part_results for details."
            ),
        }

        status_updated = update_story_status(
            story,
            overall,
            summary,
        )

        if not status_updated:
            print("❌ Could not verify final MongoDB story status", flush=True)
            return 1

        print("\n==========================================", flush=True)
        print(f"🏁 LONG STORY FINISHED: {overall}", flush=True)
        print(f"✅ Successful parts: {successful_parts}", flush=True)
        print(f"❌ Failed parts    : {failed_parts}", flush=True)
        print("==========================================", flush=True)

        # Return non-zero only when every part failed. A partial success is a
        # valid completed run because the failed parts are recorded in MongoDB
        # and all remaining parts were still attempted.
        return 1 if overall == "FAILED" else 0

    except Exception as exc:
        print(f"❌ Long-story orchestration failed: {exc}", flush=True)

        if story:
            update_story_status(
                story,
                "FAILED",
                {
                    "last_error": str(exc),
                    "failed_at": datetime.now(timezone.utc),
                },
            )
        return 1


if __name__ == "__main__":
    sys.exit(main())
