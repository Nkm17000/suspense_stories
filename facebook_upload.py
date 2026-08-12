"""Upload the generated video to a Facebook Page.

Required environment variables:
FB_PAGE_ID
FB_PAGE_ACCESS_TOKEN

Optional environment variables:
FB_GRAPH_API_VERSION   default: v24.0
FB_VIDEO_PATH          default: final_video.mp4
FB_POST_TITLE          default: Smart Learning Lab - New Story
FB_POST_DESCRIPTION    optional fallback description
MONGODB_URI             optional
STORY_ID               optional

The MongoDB story title is used both as the Facebook video title
and at the beginning of the Facebook Reel caption.
"""

import os
import sys
from pathlib import Path

import requests


# ============================================================
# CONFIGURATION
# ============================================================

PAGE_ID = os.getenv(
    "FB_PAGE_ID",
    ""
).strip()

ACCESS_TOKEN = os.getenv(
    "FB_PAGE_ACCESS_TOKEN",
    ""
).strip()

GRAPH_API_VERSION = os.getenv(
    "FB_GRAPH_API_VERSION",
    "v24.0"
).strip()

VIDEO_PATH = os.getenv(
    "FB_VIDEO_PATH",
    "final_video.mp4"
).strip()

DEFAULT_TITLE = os.getenv(
    "FB_POST_TITLE",
    "Smart Learning Lab - New Story"
).strip()

DEFAULT_DESCRIPTION = os.getenv(
    "FB_POST_DESCRIPTION",
    "Watch this inspiring story from Smart Learning Lab."
).strip()

MONGODB_URI = os.getenv(
    "MONGODB_URI",
    ""
).strip()

STORY_ID = os.getenv(
    "STORY_ID",
    ""
).strip()


# ============================================================
# VALIDATION
# ============================================================

def validate_configuration():

    if not PAGE_ID:
        raise RuntimeError(
            "FB_PAGE_ID is not configured."
        )

    if not ACCESS_TOKEN:
        raise RuntimeError(
            "FB_PAGE_ACCESS_TOKEN is not configured."
        )

    video_file = Path(
        VIDEO_PATH
    )

    if not video_file.exists():
        raise FileNotFoundError(
            f"Video file not found: {VIDEO_PATH}"
        )

    if video_file.stat().st_size == 0:
        raise RuntimeError(
            f"Video file is empty: {VIDEO_PATH}"
        )


# ============================================================
# MONGODB STORY TITLE
# ============================================================

def get_story_title_from_mongodb():
    """
    Read the title for STORY_ID from MongoDB.

    Returns None when MongoDB lookup is unavailable.
    """

    if not MONGODB_URI or not STORY_ID:
        return None

    try:

        from pymongo import MongoClient

        print(
            f"🔎 Reading story title for {STORY_ID}...",
            flush=True
        )

        client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=10000
        )

        try:

            client.admin.command(
                "ping"
            )

            db = client[
                "storydb"
            ]

            collection = db[
                "longstory"
            ]

            story = collection.find_one(
                {
                    "story_id": STORY_ID
                },
                {
                    "title": 1
                }
            )

            if story:

                title = story.get(
                    "title"
                )

                if title:

                    print(
                        f"✅ MongoDB story title: "
                        f"{title}",
                        flush=True
                    )

                    return str(
                        title
                    ).strip()

            print(
                "⚠️ Story title was not found "
                "in MongoDB.",
                flush=True
            )

            return None

        finally:

            client.close()

    except Exception as exc:

        print(
            f"⚠️ MongoDB title lookup failed: "
            f"{exc}",
            flush=True
        )

        return None


# ============================================================
# FACEBOOK REEL CAPTION
# ============================================================

def build_caption(title, part_no=None, part_title=None):
    """
    Build a concise Facebook Reel caption.

    The story title is always placed first so users immediately
    see the actual story being published.
    """

    part_line = f"भाग {int(part_no)}" if part_no is not None else ""
    part_title_line = f"{part_title}" if part_title else ""
    caption = (
        f"🦊 {title}\n\n"
        f"{part_line}\n\n" if part_line else f"🦊 {title}\n\n"
    )
    if part_title_line:
        caption += f"{part_title_line}\n\n"
    caption += (
        "पूरी कहानी देखें और अंत तक जरूर रुकें। 🎬\n\n"
        "❤️ Like  |  💬 Comment  |  🔔 Follow\n\n"
        "#HindiStory #HindiReels #HeartTouchingStory "
        "#InspiringStory #SmartLearningLab"
    )

    return caption.strip()


# ============================================================
# FACEBOOK UPLOAD
# ============================================================

def upload_video(
    video_path=None,
    story_title=None,
    part_no=None,
    part_title=None,
    story_id=None,
):
    """Upload one part video and return the Facebook video ID."""
    global VIDEO_PATH, STORY_ID

    if video_path:
        video_path = str(video_path)
    else:
        video_path = VIDEO_PATH

    if story_id:
        STORY_ID = str(story_id).strip()

    validate_video_path = Path(video_path)
    if not validate_video_path.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")
    if validate_video_path.stat().st_size == 0:
        raise RuntimeError(f"Video file is empty: {video_path}")

    if not PAGE_ID:
        raise RuntimeError("FB_PAGE_ID is not configured.")
    if not ACCESS_TOKEN:
        raise RuntimeError("FB_PAGE_ACCESS_TOKEN is not configured.")

    title = (
        str(story_title).strip()
        if story_title
        else get_story_title_from_mongodb()
        or DEFAULT_TITLE
    )

    if part_no is not None:
        facebook_title = f"{title} - भाग {int(part_no)}"
    else:
        facebook_title = title

    if len(facebook_title) > 255:
        facebook_title = facebook_title[:252] + "..."

    description = build_caption(
        title,
        part_no=part_no,
        part_title=part_title,
    )
    if not description:
        description = DEFAULT_DESCRIPTION

    print("==========================================", flush=True)
    print("Facebook Page Video Upload", flush=True)
    print("==========================================", flush=True)
    print(f"Part         : {part_no if part_no is not None else 'N/A'}", flush=True)
    print(f"Video        : {validate_video_path}", flush=True)
    print(
        f"Video size   : {validate_video_path.stat().st_size / (1024 * 1024):.2f} MB",
        flush=True,
    )
    print(f"Graph API    : {GRAPH_API_VERSION}", flush=True)
    print(f"Title        : {facebook_title}", flush=True)
    print("Caption:", flush=True)
    print(description, flush=True)
    print("Uploading video to Facebook Page...", flush=True)

    endpoint = (
        f"https://graph.facebook.com/"
        f"{GRAPH_API_VERSION}/"
        f"{PAGE_ID}/videos"
    )
    params = {"access_token": ACCESS_TOKEN}
    data = {
        "title": facebook_title,
        "description": description,
        "published": True
    }

    try:
        with validate_video_path.open("rb") as video:
            response = requests.post(
                endpoint,
                params=params,
                data=data,
                files={
                    "source": (
                        validate_video_path.name,
                        video,
                        "video/mp4",
                    )
                },
                timeout=1800,
            )
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Facebook upload request failed: {exc}"
        ) from exc

    print(f"Facebook HTTP status: {response.status_code}", flush=True)

    try:
        result = response.json()
    except ValueError:
        result = {"raw_response": response.text}

    if not response.ok:
        print("❌ Facebook API response:", result, flush=True)
        raise RuntimeError("Facebook video upload failed.")

    video_id = result.get("id")
    if not video_id:
        print("❌ Facebook returned no video ID.", flush=True)
        print("Response:", result, flush=True)
        raise RuntimeError("Facebook upload did not return a video ID.")

    print("==========================================", flush=True)
    print("✅ Facebook video uploaded successfully", flush=True)
    print(f"Facebook video ID: {video_id}", flush=True)
    print("==========================================", flush=True)
    return video_id


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    try:

        upload_video()

    except Exception as exc:

        print(
            f"❌ Facebook upload failed: "
            f"{exc}",
            flush=True
        )

        sys.exit(1)
