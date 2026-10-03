"""Publish one generated inspirational story video to Facebook and Instagram."""

import os
import sys

from facebook_upload import upload_video as upload_video_to_facebook
from instagram_service import publish_video_to_instagram

VIDEO_PATH = os.getenv("SOCIAL_VIDEO_PATH", os.getenv("FB_VIDEO_PATH", "final_video.mp4")).strip()
STORY_TITLE = os.getenv("STORY_TITLE", "Smart Learning Lab - New Story").strip()


def build_instagram_caption(title: str) -> str:
    return (
        f"🦊 {title}\n\n"
        "पूरी कहानी देखें और अंत तक जरूर रुकें। 🎬\n\n"
        "❤️ Like  |  💬 Comment  |  🔔 Follow\n\n"
        "#HindiStory #HindiReels #HeartTouchingStory "
        "#InspiringStory #SmartLearningLab"
    ).strip()


def main() -> int:
    print("=" * 70, flush=True)
    print("📱 SOCIAL PUBLISHER — FACEBOOK + INSTAGRAM", flush=True)
    print("=" * 70, flush=True)
    print(f"Video: {VIDEO_PATH}", flush=True)
    print(f"Title: {STORY_TITLE}", flush=True)

    facebook_ok = False
    instagram_ok = False
    instagram_skipped = False

    # ------------------------------------------------------------
    # Facebook
    # ------------------------------------------------------------
    try:
        print("\n📘 Publishing to Facebook Page...", flush=True)
        fb_result = upload_video_to_facebook()
        facebook_ok = bool(fb_result)
        if facebook_ok:
            print(f"✅ Facebook published: {fb_result}", flush=True)
    except Exception as exc:
        print(f"❌ Facebook publish failed: {exc}", flush=True)

    # ------------------------------------------------------------
    # Instagram
    # ------------------------------------------------------------
    ig_id = os.getenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", "").strip()
    ig_token = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()

    if not ig_id or not ig_token:
        print(
            "⚠️ Instagram credentials are not configured. "
            "Set INSTAGRAM_BUSINESS_ACCOUNT_ID and INSTAGRAM_ACCESS_TOKEN.",
            flush=True,
        )
    else:
        try:
            print("\n📸 Publishing to Instagram Reel...", flush=True)
            ig_result = publish_video_to_instagram(
                VIDEO_PATH,
                build_instagram_caption(STORY_TITLE),
            )
            if ig_result is None:
                instagram_skipped = True
                print("⏭️ Instagram was skipped because Meta reported a publishing limit.", flush=True)
            else:
                instagram_ok = True
                print(f"✅ Instagram published: {ig_result}", flush=True)
        except Exception as exc:
            print(f"❌ Instagram publish failed: {exc}", flush=True)

    print("\n" + "=" * 70, flush=True)
    print("PUBLISH SUMMARY", flush=True)
    print(f"Facebook : {'SUCCESS' if facebook_ok else 'FAILED'}", flush=True)
    if instagram_skipped:
        print("Instagram: SKIPPED (publishing limit)", flush=True)
    else:
        print(f"Instagram: {'SUCCESS' if instagram_ok else 'FAILED'}", flush=True)
    print("=" * 70, flush=True)

    # One platform succeeding is enough to consider the publishing step
    # successful. This prevents a temporary Meta Instagram limit from
    # deleting/invalidating an otherwise successful Facebook publication.
    if facebook_ok or instagram_ok:
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
