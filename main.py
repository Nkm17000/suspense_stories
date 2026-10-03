"""Resumable suspense-story generator: one unfinished part per GitHub Actions run."""
import os
import sys
from datetime import datetime, timezone

from smart_video.db import (
    get_story_from_mongodb,
    update_part_status,
    update_story_status,
)
from smart_video.video_builder import build_part_video
from smart_video.image_generator import start_story_usage, save_usage_report
from facebook_upload import upload_video
from instagram_service import publish_video_to_instagram


def export_story_to_github_actions(story_id, title):
    github_env = os.getenv("GITHUB_ENV")
    if not github_env:
        return
    delimiter = "STORY_VALUE_DELIMITER_9f3a7c"
    with open(github_env, "a", encoding="utf-8") as f:
        f.write(f"STORY_ID<<{delimiter}\n{story_id}\n{delimiter}\n")
        f.write(f"STORY_TITLE<<{delimiter}\n{title}\n{delimiter}\n")


def build_instagram_caption(title, part_no, part_title):
    return (
        f"🎬 {title}\n\n"
        f"भाग {part_no}: {part_title}\n\n"
        "अंत तक जरूर देखें... कहानी का रहस्य आखिरी पल में खुलता है। 🔥\n\n"
        "❤️ Like  |  💬 Comment  |  🔔 Follow\n\n"
        "#HindiStory #SuspenseStory #HindiReels #SuspenseReels "
        "#MysteryStory #SmartLearningLab"
    ).strip()


def _mark_part_pending(story, part_no, error, video_path=None,
                       facebook_status=None, facebook_video_id=None,
                       instagram_status=None, instagram_media_id=None):
    fields = {
        "error": str(error) if error else None,
        "video_path": video_path,
        "updated_at": datetime.now(timezone.utc),
    }
    if facebook_status is not None:
        fields["facebook_status"] = facebook_status
    if facebook_video_id is not None:
        fields["facebook_video_id"] = facebook_video_id
    if instagram_status is not None:
        fields["instagram_status"] = instagram_status
    if instagram_media_id is not None:
        fields["instagram_media_id"] = instagram_media_id
    update_part_status(story, part_no, "PENDING", fields)


def _is_posted(value):
    return str(value or "").upper() == "POSTED"


def main():
    print("🚀 Starting Smart Learning Lab suspense-story generator...", flush=True)
    story = None

    try:
        story, selected_parts = get_story_from_mongodb()
        if not story:
            print("ℹ️ No PROCESSING or PENDING suspense story was found.", flush=True)
            return 0

        if not selected_parts:
            print("⚠️ Selected story has no unfinished part in this run.", flush=True)
            return 0

        # Exactly ONE part per GitHub Actions run.
        part = selected_parts[0]
        story_id = story.get("story_id") or story.get("id") or story.get("ID") or "unknown"
        title = str(story.get("title") or "Untitled Suspense Story").strip()
        part_no = int(part["part_no"])
        part_title = str(part.get("part_title") or f"भाग {part_no}").strip()
        scenes = part.get("scenes") or []

        print("=" * 60, flush=True)
        print(f"🆔 Story ID : {story_id}", flush=True)
        print(f"📖 Title   : {title}", flush=True)
        print(f"🧩 Total parts in story: {len(story.get('parts') or [])}", flush=True)
        print(f"▶️ Selected part: {part_no}", flush=True)
        print(f"🎬 Scene count: {len(scenes)}", flush=True)
        print("=" * 60, flush=True)

        if not scenes:
            # No fixed count is required, but a part must contain something to render.
            error = f"Part {part_no} contains no scenes; nothing can be rendered."
            print(f"⚠️ {error}", flush=True)
            _mark_part_pending(story, part_no, error)
            update_story_status(story, "PROCESSING", {"last_error": error})
            return 0

        export_story_to_github_actions(story_id, title)
        start_story_usage(story_id=story_id, story_title=title)

        update_part_status(
            story,
            part_no,
            "PROCESSING",
            {
                "error": None,
                "started_at": datetime.now(timezone.utc),
            },
        )

        # Read the current platform statuses after the status update. Existing
        # POSTED platforms must never be posted again on a retry.
        result = (story.get("part_results") or {}).get(f"part_{part_no:02d}") or {}
        fb_status = str(result.get("facebook_status") or "PENDING").upper()
        ig_status = str(result.get("instagram_status") or "PENDING").upper()
        fb_id = result.get("facebook_video_id")
        ig_id = result.get("instagram_media_id")
        video_path = None

        try:
            # Video is built for this part only. Part/scene counts are dynamic.
            video_path = build_part_video(
                scenes=scenes,
                title=title,
                part_no=part_no,
                part_title=part_title,
            )

            # ------------------------------------------------------------
            # Facebook: only if not already POSTED.
            # ------------------------------------------------------------
            if not _is_posted(fb_status):
                try:
                    print(f"📘 Publishing Part {part_no} to Facebook...", flush=True)
                    fb_id = upload_video(
                        video_path=video_path,
                        story_title=title,
                        part_no=part_no,
                        part_title=part_title,
                        story_id=story_id,
                    )
                    fb_status = "POSTED" if fb_id else "FAILED"
                    print(f"📘 Facebook: {fb_status}", flush=True)
                except Exception as exc:
                    fb_status = "FAILED"
                    print(f"❌ Facebook publish failed: {exc}", flush=True)
            else:
                print(f"⏭️ Facebook already POSTED; skipping Part {part_no}.", flush=True)

            update_part_status(
                story, part_no, "PROCESSING",
                {
                    "video_path": video_path,
                    "facebook_status": fb_status,
                    "facebook_video_id": fb_id,
                },
            )

            # ------------------------------------------------------------
            # Instagram: only if not already POSTED.
            # ------------------------------------------------------------
            if not _is_posted(ig_status):
                try:
                    ig_account = os.getenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", "").strip()
                    ig_token = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()
                    if not ig_account or not ig_token:
                        ig_status = "FAILED"
                        print("⚠️ Instagram credentials not configured.", flush=True)
                    else:
                        print(f"📸 Publishing Part {part_no} to Instagram Reel...", flush=True)
                        ig_result = publish_video_to_instagram(
                            video_path,
                            build_instagram_caption(title, part_no, part_title),
                        )
                        ig_id = ((ig_result or {}).get("id")
                                 if isinstance(ig_result, dict) else ig_result)
                        ig_status = "POSTED" if ig_id else "FAILED"
                        print(f"📸 Instagram: {ig_status}", flush=True)
                except Exception as exc:
                    ig_status = "FAILED"
                    print(f"❌ Instagram publish failed: {exc}", flush=True)
            else:
                print(f"⏭️ Instagram already POSTED; skipping Part {part_no}.", flush=True)

            # ------------------------------------------------------------
            # A part is SUCCESS only when BOTH required platforms are POSTED.
            # ------------------------------------------------------------
            if _is_posted(fb_status) and _is_posted(ig_status):
                update_part_status(
                    story, part_no, "SUCCESS",
                    {
                        "video_path": video_path,
                        "facebook_status": fb_status,
                        "facebook_video_id": fb_id,
                        "instagram_status": ig_status,
                        "instagram_media_id": ig_id,
                        "error": None,
                        "completed_at": datetime.now(timezone.utc),
                    },
                )
                print(f"✅ PART {part_no} SUCCESS — both platforms POSTED", flush=True)
            else:
                error = (
                    f"Part {part_no} incomplete: "
                    f"Facebook={fb_status}, Instagram={ig_status}. "
                    "Part remains PENDING for the next run."
                )
                print(f"🔄 {error}", flush=True)
                _mark_part_pending(
                    story, part_no, error,
                    video_path=video_path,
                    facebook_status=fb_status,
                    facebook_video_id=fb_id,
                    instagram_status=ig_status,
                    instagram_media_id=ig_id,
                )

        except Exception as exc:
            error = f"Part {part_no} processing failed: {exc}"
            print(f"❌ {error}", flush=True)
            _mark_part_pending(
                story, part_no, error,
                video_path=video_path,
                facebook_status=fb_status,
                facebook_video_id=fb_id,
                instagram_status=ig_status,
                instagram_media_id=ig_id,
            )
            # Do not mark the whole story FAILED. It is intentionally
            # recoverable by the next scheduled/manual GitHub Actions run.

        report_path, usage = save_usage_report()
        print("\n📊 IMAGE / CLOUDFLARE USAGE", flush=True)
        print(f"🖼️ Images requested: {len(usage['images'])}", flush=True)
        print(f"☁️ Cloudflare successes: {usage['cloudflare_successes']}", flush=True)
        print(f"🔄 Pollinations fallbacks: {usage['pollinations_successes']}", flush=True)
        print(f"🔢 Cloudflare attempts: {usage['cloudflare_attempts']}", flush=True)
        print(f"🧮 Cloudflare neurons: {usage['total_cloudflare_neurons']:.2f}", flush=True)
        print(f"📄 Usage report: {report_path}", flush=True)

        # The next run will re-query MongoDB. If this part succeeded, it will
        # select the next unfinished part of the same PROCESSING story first.
        update_story_status(
            story,
            "PROCESSING",
            {
                "image_usage": usage,
                "usage_report_path": report_path,
                "updated_at": datetime.now(timezone.utc),
            },
        )

        print(
            f"🏁 RUN FINISHED: story={story_id}, part={part_no}. "
            "Next run will resume PROCESSING work before taking a new PENDING story.",
            flush=True,
        )
        return 0

    except Exception as exc:
        print(f"❌ Suspense-story orchestration failed: {exc}", flush=True)
        if story:
            update_story_status(
                story,
                "PROCESSING",
                {
                    "last_error": str(exc),
                    "updated_at": datetime.now(timezone.utc),
                },
            )
        return 1


if __name__ == "__main__":
    sys.exit(main())
