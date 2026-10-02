"""Sequential suspense-story video generator with Facebook + Instagram publishing."""
import os
import sys
from datetime import datetime, timezone

from smart_video.db import get_story_from_mongodb, update_part_status, update_story_status
from smart_video.video_builder import build_part_video
from smart_video.image_generator import start_story_usage, save_usage_report, get_usage_summary
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


def _mark_part_failed(story, part_no, error, video_path=None,
                      facebook_status="FAILED", instagram_status="FAILED"):
    update_part_status(
        story, part_no, "FAILED",
        {
            "error": str(error),
            "video_path": video_path,
            "facebook_status": facebook_status,
            "instagram_status": instagram_status,
            "failed_at": datetime.now(timezone.utc),
        },
    )


def main():
    print("🚀 Starting Smart Learning Lab suspense-story generator...", flush=True)
    story = None
    successful_parts = failed_parts = 0

    try:
        story, parts = get_story_from_mongodb()
        if not story:
            print("ℹ️ No PENDING suspense story was found.", flush=True)
            return 2

        story_id = story.get("story_id") or story.get("id") or story.get("ID") or "unknown"
        title = str(story.get("title") or "Untitled Suspense Story").strip()

        print("=" * 60)
        print(f"🆔 Story ID : {story_id}")
        print(f"📖 Title   : {title}")
        print(f"🧩 Parts   : {len(parts)}")
        print("=" * 60)

        export_story_to_github_actions(story_id, title)
        start_story_usage(story_id=story_id, story_title=title)

        for part in parts:
            part_no = int(part["part_no"])
            part_title = str(part.get("part_title") or f"भाग {part_no}").strip()
            scenes = part.get("scenes") or []

            print("\n" + "=" * 60)
            print(f"▶️ START PART {part_no}: {part_title}")
            print(f"🎬 Scenes: {len(scenes)}")
            print("=" * 60)

            update_part_status(
                story, part_no, "PROCESSING",
                {
                    "error": None,
                    "started_at": datetime.now(timezone.utc),
                    "facebook_status": "PROCESSING",
                    "instagram_status": "PROCESSING",
                },
            )

            if len(scenes) != 10:
                error = f"Part {part_no} must contain exactly 10 scenes; found {len(scenes)}"
                _mark_part_failed(story, part_no, error)
                failed_parts += 1
                continue

            video_path = None
            fb_id = None
            ig_id = None
            fb_status = "FAILED"
            ig_status = "FAILED"

            try:
                video_path = build_part_video(
                    scenes=scenes, title=title, part_no=part_no, part_title=part_title
                )

                # Publish to Facebook first.
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
                    print(f"❌ Facebook publish failed: {exc}", flush=True)
                    fb_status = "FAILED"

                # Publish the same finished MP4 as an Instagram Reel.
                try:
                    ig_account = os.getenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", "").strip()
                    ig_token = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()
                    if not ig_account or not ig_token:
                        print("⚠️ Instagram credentials not configured; skipping.", flush=True)
                        ig_status = "SKIPPED"
                    else:
                        print(f"📸 Publishing Part {part_no} to Instagram Reel...", flush=True)
                        ig_result = publish_video_to_instagram(
                            video_path,
                            build_instagram_caption(title, part_no, part_title),
                        )
                        ig_id = (ig_result or {}).get("id") if isinstance(ig_result, dict) else ig_result
                        ig_status = "POSTED" if ig_id else "SKIPPED"
                except Exception as exc:
                    print(f"❌ Instagram publish failed: {exc}", flush=True)
                    ig_status = "FAILED"

                if fb_status == "POSTED" or ig_status == "POSTED":
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
                    successful_parts += 1
                    print(f"✅ PART {part_no} SUCCESS", flush=True)
                else:
                    raise RuntimeError("Video was created but neither Facebook nor Instagram publishing succeeded.")

            except Exception as exc:
                failed_parts += 1
                print(f"❌ PART {part_no} FAILED: {exc}", flush=True)
                _mark_part_failed(
                    story, part_no, exc, video_path=video_path,
                    facebook_status=fb_status, instagram_status=ig_status,
                )

        report_path, usage = save_usage_report()
        print("\n📊 IMAGE / CLOUDFLARE USAGE", flush=True)
        print(f"🖼️ Images requested: {len(usage['images'])}", flush=True)
        print(f"☁️ Cloudflare successes: {usage['cloudflare_successes']}", flush=True)
        print(f"🔄 Pollinations fallbacks: {usage['pollinations_successes']}", flush=True)
        print(f"🔢 Cloudflare attempts: {usage['cloudflare_attempts']}", flush=True)
        print(f"🧮 Cloudflare neurons used/estimated: {usage['total_cloudflare_neurons']:.2f}", flush=True)
        print(f"📄 Usage report: {report_path}", flush=True)

        overall = "SUCCESS" if failed_parts == 0 and successful_parts > 0 else (
            "PARTIAL_SUCCESS" if successful_parts > 0 else "FAILED"
        )
        ok = update_story_status(
            story, overall,
            {
                "completed_at": datetime.now(timezone.utc),
                "last_error": None if failed_parts == 0 else f"{failed_parts} part(s) failed.",
                "image_usage": usage,
                "usage_report_path": report_path,
            },
        )
        if not ok:
            return 1

        print(f"🏁 SUSPENSE STORY FINISHED: {overall}", flush=True)
        return 1 if overall == "FAILED" else 0

    except Exception as exc:
        print(f"❌ Suspense-story orchestration failed: {exc}", flush=True)
        if story:
            update_story_status(story, "FAILED", {
                "last_error": str(exc),
                "failed_at": datetime.now(timezone.utc),
            })
        return 1


if __name__ == "__main__":
    sys.exit(main())
