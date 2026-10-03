"""Instagram Reels publisher adapted from the working quiz-agent flow."""

import json
import os
import time
from pathlib import Path

import requests


META_GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v23.0").strip()
INSTAGRAM_BUSINESS_ACCOUNT_ID = os.getenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", "").strip()
INSTAGRAM_ACCESS_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()

MAX_REEL_BYTES = 1_000 * 1024 * 1024
GRAPH_BASE = f"https://graph.facebook.com/{META_GRAPH_VERSION}"

PUBLISHING_LIMIT_CODE = 9
PUBLISHING_LIMIT_SUBCODES = {2207069}
INSTAGRAM_UPLOAD_MAX_ATTEMPTS = 3
INSTAGRAM_UPLOAD_RETRY_DELAYS = (10, 20)


def _require_config():
    if not INSTAGRAM_ACCESS_TOKEN:
        raise ValueError("INSTAGRAM_ACCESS_TOKEN is missing.")
    if not INSTAGRAM_BUSINESS_ACCOUNT_ID:
        raise ValueError("INSTAGRAM_BUSINESS_ACCOUNT_ID is missing.")


def _response_details(response: requests.Response) -> str:
    try:
        return json.dumps(response.json(), ensure_ascii=False)
    except ValueError:
        return response.text[:4000]


def _meta_error_payload(response: requests.Response):
    try:
        payload = response.json()
        return payload.get("error") or {}
    except ValueError:
        return {}


def _is_publishing_limit_error(response: requests.Response) -> bool:
    error = _meta_error_payload(response)
    try:
        code = int(error.get("code", -1))
    except (TypeError, ValueError):
        code = -1
    try:
        subcode = int(error.get("error_subcode", -1))
    except (TypeError, ValueError):
        subcode = -1
    return code == PUBLISHING_LIMIT_CODE and subcode in PUBLISHING_LIMIT_SUBCODES


def _raise_meta_error(response: requests.Response, action: str):
    if response.ok:
        return
    raise RuntimeError(
        f"Instagram {action} failed: HTTP {response.status_code}. "
        f"Meta response: {_response_details(response)}"
    )


def _validate_account():
    url = f"{GRAPH_BASE}/{INSTAGRAM_BUSINESS_ACCOUNT_ID}"
    response = requests.get(
        url,
        params={
            "fields": "id,username",
            "access_token": INSTAGRAM_ACCESS_TOKEN,
        },
        timeout=60,
    )
    _raise_meta_error(response, "account validation")

    data = response.json()
    returned_id = str(data.get("id", ""))
    username = data.get("username") or "unknown"

    if returned_id != str(INSTAGRAM_BUSINESS_ACCOUNT_ID):
        raise RuntimeError(
            "Instagram account validation returned a different account ID. "
            f"Configured={INSTAGRAM_BUSINESS_ACCOUNT_ID}, returned={returned_id}"
        )

    print(f"✅ Instagram account validated: @{username}", flush=True)


def _check_publishing_limit():
    url = f"{GRAPH_BASE}/{INSTAGRAM_BUSINESS_ACCOUNT_ID}/content_publishing_limit"
    try:
        response = requests.get(
            url,
            params={
                "fields": "config,quota_usage",
                "access_token": INSTAGRAM_ACCESS_TOKEN,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        print(f"ℹ️ Instagram publishing-limit preflight unavailable: {exc}", flush=True)
        return False

    if not response.ok:
        print(
            "ℹ️ Instagram publishing-limit preflight unavailable; "
            "continuing to the normal publish request.",
            flush=True,
        )
        return False

    try:
        data = response.json()
    except ValueError:
        return False

    config = data.get("config") or {}
    quota_usage = data.get("quota_usage")
    limit = config.get("quota_total") or config.get("limit") or config.get("max_posts")

    try:
        if limit is not None and quota_usage is not None:
            usage = float(quota_usage)
            maximum = float(limit)
            if maximum > 0 and usage >= maximum:
                print(
                    f"⛔ Instagram Content Publishing quota reached ({usage:g}/{maximum:g}).",
                    flush=True,
                )
                return True
    except (TypeError, ValueError):
        pass

    return False


def _create_resumable_container(caption):
    url = f"{GRAPH_BASE}/{INSTAGRAM_BUSINESS_ACCOUNT_ID}/media"
    response = requests.post(
        url,
        data={
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption,
            "share_to_feed": "true",
            "access_token": INSTAGRAM_ACCESS_TOKEN,
        },
        timeout=60,
    )

    if _is_publishing_limit_error(response):
        error = _meta_error_payload(response)
        message = error.get("error_user_msg") or error.get("message") or "Media creation limit exceeded"
        print(f"⛔ Instagram Content Publishing API limit reached. Meta: {message}", flush=True)
        return None, None

    _raise_meta_error(response, "Reel container creation")

    data = response.json()
    container_id = data.get("id")
    upload_uri = data.get("uri")

    if not container_id or not upload_uri:
        raise RuntimeError(
            "Instagram Reel container response is incomplete: "
            f"{json.dumps(data, ensure_ascii=False)}"
        )

    print(f"📦 Instagram Reel container created: {container_id}", flush=True)
    return container_id, upload_uri


def _upload_video(upload_uri, video_path):
    path = Path(video_path)
    if not path.is_file():
        raise FileNotFoundError(f"Instagram video not found: {path}")

    file_size = path.stat().st_size
    if file_size <= 0:
        raise ValueError(f"Instagram video is empty: {path}")
    if file_size > MAX_REEL_BYTES:
        raise ValueError(
            f"Instagram Reel is {file_size / 1024 / 1024:.1f} MB; "
            f"maximum supported size is {MAX_REEL_BYTES / 1024 / 1024:.0f} MB."
        )

    headers = {
        "Authorization": f"OAuth {INSTAGRAM_ACCESS_TOKEN}",
        "offset": "0",
        "file_size": str(file_size),
        "Content-Type": "video/mp4",
    }

    print(f"📤 Uploading video to Instagram: {file_size / 1024 / 1024:.1f} MB", flush=True)
    with path.open("rb") as video_file:
        response = requests.post(
            upload_uri,
            headers=headers,
            data=video_file,
            timeout=900,
        )

    _raise_meta_error(response, "binary video upload")

    try:
        result = response.json()
    except ValueError:
        result = {"raw_response": response.text[:4000]}

    if result.get("success") is not True:
        raise RuntimeError(f"Instagram binary upload was not successful: {result}")

    print("✅ Instagram video upload completed", flush=True)


def _wait_until_ready(container_id, timeout_seconds=900, poll_seconds=10):
    url = f"{GRAPH_BASE}/{container_id}"
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        response = requests.get(
            url,
            params={
                "fields": "status_code,status",
                "access_token": INSTAGRAM_ACCESS_TOKEN,
            },
            timeout=60,
        )
        _raise_meta_error(response, "container status check")

        data = response.json()
        status = data.get("status_code") or data.get("status")
        print(f"⏳ Instagram processing status: {status}", flush=True)

        if status == "FINISHED":
            return
        if status in {"ERROR", "EXPIRED"}:
            raise RuntimeError(
                "Instagram video processing failed: "
                f"{json.dumps(data, ensure_ascii=False)}"
            )

        time.sleep(poll_seconds)

    raise TimeoutError(
        f"Instagram Reel container {container_id} did not finish within "
        f"{timeout_seconds} seconds."
    )


def _publish_container(container_id):
    url = f"{GRAPH_BASE}/{INSTAGRAM_BUSINESS_ACCOUNT_ID}/media_publish"
    response = requests.post(
        url,
        data={
            "creation_id": container_id,
            "access_token": INSTAGRAM_ACCESS_TOKEN,
        },
        timeout=60,
    )
    _raise_meta_error(response, "Reel publishing")

    result = response.json()
    media_id = result.get("id")
    if not media_id:
        raise RuntimeError(
            "Instagram publish response has no media ID: "
            f"{json.dumps(result, ensure_ascii=False)}"
        )

    print(f"📸 Instagram Reel published: {media_id}", flush=True)
    return result


def publish_video_to_instagram(video_path, caption):
    """Publish one local MP4 as an Instagram Reel with up to 3 upload attempts."""
    _require_config()
    _validate_account()

    if _check_publishing_limit():
        print("⏭️ Skipping Instagram publish because the Content Publishing limit is exhausted.", flush=True)
        return None

    last_error = None

    for attempt in range(1, INSTAGRAM_UPLOAD_MAX_ATTEMPTS + 1):
        container_id = None
        try:
            print(
                f"📤 Instagram video upload attempt {attempt}/{INSTAGRAM_UPLOAD_MAX_ATTEMPTS}",
                flush=True,
            )

            # Create a fresh resumable container for every attempt.
            # An upload URI belongs to its container and should not be reused
            # after a failed binary upload.
            container_id, upload_uri = _create_resumable_container(caption)
            if not container_id or not upload_uri:
                return None

            _upload_video(upload_uri, video_path)
            _wait_until_ready(container_id)
            return _publish_container(container_id)

        except Exception as exc:
            last_error = exc
            print(
                f"❌ Instagram upload attempt {attempt}/{INSTAGRAM_UPLOAD_MAX_ATTEMPTS} failed: {exc}",
                flush=True,
            )

            if attempt < INSTAGRAM_UPLOAD_MAX_ATTEMPTS:
                delay = INSTAGRAM_UPLOAD_RETRY_DELAYS[attempt - 1]
                print(f"⏳ Waiting {delay} seconds before Instagram retry...", flush=True)
                time.sleep(delay)

    raise RuntimeError(
        f"Instagram video upload failed after {INSTAGRAM_UPLOAD_MAX_ATTEMPTS} attempts: {last_error}"
    ) from last_error
