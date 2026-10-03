"""AI image generation with multi-account Cloudflare Workers AI and Pollinations fallback."""

import base64
import json
import os
import time
from datetime import datetime, timezone
import urllib.parse
from io import BytesIO

import requests
from PIL import Image, ImageDraw

from .config import VIDEO_SIZE
from .fonts import get_unicode_font
from .cloudflare_accounts import (
    choose_account,
    settle_reserved_neurons,
    release_reserved_neurons,
    record_image,
    mark_account_exhausted,
)


# ============================================================
# CONFIGURATION
# ============================================================

CLOUDFLARE_MODEL = os.getenv(
    "CLOUDFLARE_IMAGE_MODEL",
    "@cf/black-forest-labs/flux-1-schnell",
).strip()

# 3 steps keeps usage lower while retaining reasonable scene quality.
# Increase to 4 if you prefer the model's default quality/steps.
CLOUDFLARE_STEPS = int(
    os.getenv("CLOUDFLARE_IMAGE_STEPS", "1")
)

CLOUDFLARE_RETRIES = int(
    os.getenv("CLOUDFLARE_IMAGE_RETRIES", "2")
)

CLOUDFLARE_TIMEOUT = int(
    os.getenv("CLOUDFLARE_IMAGE_TIMEOUT", "120")
)

# Cloudflare accepts prompts up to 2048 characters. Keep a safety margin.
CLOUDFLARE_MAX_PROMPT_CHARS = int(os.getenv("CLOUDFLARE_MAX_PROMPT_CHARS", "1800"))

POLLINATIONS_RETRIES = int(
    os.getenv("POLLINATIONS_IMAGE_RETRIES", "3")
)

POLLINATIONS_TIMEOUT = int(
    os.getenv("POLLINATIONS_IMAGE_TIMEOUT", "30")
)

# Cloudflare FLUX.1 Schnell usage estimate. Cloudflare responses for this
# model normally contain the Base64 image but do not expose neuron usage in
# the REST response, so the tracker records an ESTIMATED value unless an
# explicit neuron field is returned by the API.
CLOUDFLARE_BASE_NEURONS_PER_TILE = float(
    os.getenv("CLOUDFLARE_BASE_NEURONS_PER_TILE", "4.8")
)
CLOUDFLARE_NEURONS_PER_STEP = float(
    os.getenv("CLOUDFLARE_NEURONS_PER_STEP", "9.6")
)

# Cloudflare's REST image response normally does not expose a documented
# per-request neuron count, so routing uses the exact published FLUX.1
# Schnell neuron formula configured below. MongoDB is the persistent
# account/day ledger used for multi-account routing.
_USAGE = {
    "story_id": None,
    "story_title": None,
    "started_at": None,
    "images": [],
    "cloudflare_attempts": 0,
    "cloudflare_successes": 0,
    "pollinations_successes": 0,
    "local_fallbacks": 0,
    "estimated_cloudflare_neurons": 0.0,
    "reported_cloudflare_neurons": 0.0,
}


def start_story_usage(story_id=None, story_title=None):
    """Reset image/neuron accounting for one complete story run."""
    global _USAGE
    _USAGE = {
        "story_id": story_id,
        "story_title": story_title,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "images": [],
        "cloudflare_attempts": 0,
        "cloudflare_successes": 0,
        "pollinations_successes": 0,
        "local_fallbacks": 0,
        "estimated_cloudflare_neurons": 0.0,
        "reported_cloudflare_neurons": 0.0,
    }


def _extract_reported_neurons(data):
    """Find an explicit neuron count if Cloudflare ever returns one."""
    if not isinstance(data, dict):
        return None

    preferred_keys = {
        "neurons", "neuron", "neurons_used", "neuron_count",
        "total_neurons", "ai_neurons", "usage_neurons",
    }

    def walk(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if str(key).lower() in preferred_keys:
                    try:
                        value = float(value)
                        if value >= 0:
                            return value
                    except (TypeError, ValueError):
                        pass
                found = walk(value)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = walk(item)
                if found is not None:
                    return found
        return None

    return walk(data)


def _estimated_cloudflare_neurons():
    return (
        CLOUDFLARE_BASE_NEURONS_PER_TILE
        + CLOUDFLARE_NEURONS_PER_STEP * CLOUDFLARE_STEPS
    )


def _record_image_usage(
    path, provider,
    cloudflare_neurons=0.0,
    neuron_source="not_applicable",
    cloudflare_attempts=0,
    final_prompt=None,
):
    """Record provider and neuron usage for one requested image."""
    _USAGE["images"].append({
        "image_number": len(_USAGE["images"]) + 1,
        "path": path,
        "provider": provider,
        "cloudflare_neurons": round(float(cloudflare_neurons), 2),
        "neuron_source": neuron_source,
        "cloudflare_attempts": cloudflare_attempts,
        "final_prompt_sent_to_api": final_prompt,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    if provider == "cloudflare":
        _USAGE["cloudflare_successes"] += 1
        if neuron_source == "reported":
            _USAGE["reported_cloudflare_neurons"] += float(cloudflare_neurons)
        else:
            _USAGE["estimated_cloudflare_neurons"] += float(cloudflare_neurons)
    elif provider == "pollinations":
        _USAGE["pollinations_successes"] += 1
    elif provider == "local":
        _USAGE["local_fallbacks"] += 1


def get_usage_summary():
    """Return the current story image/neuron accounting."""
    estimated = round(_USAGE["estimated_cloudflare_neurons"], 2)
    reported = round(_USAGE["reported_cloudflare_neurons"], 2)
    total_known = round(estimated + reported, 2)
    return {
        **_USAGE,
        "estimated_cloudflare_neurons": estimated,
        "reported_cloudflare_neurons": reported,
        "total_cloudflare_neurons": total_known,
        "note": (
            "Cloudflare REST image responses do not normally expose neuron usage; "
            "estimated values use the configured base tile + per-step values. "
            "Check the Cloudflare dashboard for the authoritative billed total."
        ),
    }


def save_usage_report(output_dir="logs"):
    """Write a machine-readable per-image and story-level usage report."""
    os.makedirs(output_dir, exist_ok=True)
    story_id = _USAGE.get("story_id") or "unknown"
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(story_id))
    path = os.path.join(output_dir, f"neuron_usage_{safe_id}.json")
    report = get_usage_summary()
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return path, report


# ============================================================
# HELPERS
# ============================================================

def _save_cloudflare_image(response, path):
    """
    Cloudflare FLUX.1 Schnell REST responses contain:
        {
            "result": {
                "image": "<base64>"
            }
        }

    Decode the Base64 image, validate it with Pillow, and save it.
    Returns True on success.
    """
    data = response.json()

    result = data.get("result")

    if not isinstance(result, dict):
        raise ValueError(
            f"Unexpected Cloudflare result format: {type(result).__name__}"
        )

    image_base64 = result.get("image")

    if not image_base64:
        raise ValueError(
            "Cloudflare response did not contain result.image"
        )

    # Be tolerant if a future response includes a data-URI prefix.
    if image_base64.startswith("data:") and "," in image_base64:
        image_base64 = image_base64.split(",", 1)[1]

    image_bytes = base64.b64decode(image_base64, validate=True)

    # Validate before declaring success.
    image = Image.open(BytesIO(image_bytes))
    image.verify()

    with open(path, "wb") as f:
        f.write(image_bytes)

    return True


def _log_final_api_prompt(prompt, provider="cloudflare"):
    """Append the exact final prompt used for an image API request to a JSONL log."""
    os.makedirs("logs", exist_ok=True)
    story_id = _USAGE.get("story_id") or "unknown"
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(story_id))
    path = os.path.join("logs", f"image_api_prompts_{safe_id}.jsonl")
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "image_number": len(_USAGE.get("images", [])) + 1,
        "final_prompt_sent_to_api": prompt,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path



def _trim_prompt(prompt, max_chars=CLOUDFLARE_MAX_PROMPT_CHARS):
    """Keep the first N characters exactly; never fail generation because of length."""
    text = str(prompt or "")
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _generate_cloudflare(prompt, path, story_id=None, part_no=None):
    """Try every eligible Cloudflare account before returning to Pollinations.

    Rules:
      * Account order is 1, 2, 3, ...
      * A 429 containing Cloudflare quota error 4006 (or the daily-allocation
        message) marks that account exhausted for the current UTC date.
      * An exhausted account is never called again on that UTC date.
      * After an exhausted account, immediately try the next account.
      * If every configured account has been checked/excluded/unavailable,
        return False so generate_image() calls Pollinations.
    """
    prompt, was_trimmed = _trim_prompt(prompt)
    if was_trimmed:
        print(
            f"✂️ Cloudflare prompt trimmed to {CLOUDFLARE_MAX_PROMPT_CHARS} characters "
            "(kept the first characters).",
            flush=True,
        )

    estimated = _estimated_cloudflare_neurons()
    excluded_accounts = set()
    attempts_total = 0

    while True:
        account = choose_account(
            estimated,
            excluded_account_ids=excluded_accounts,
        )

        if not account:
            print(
                f"❌ All configured Cloudflare accounts checked/unavailable "
                f"for this image. Tried={len(excluded_accounts)}; "
                "switching to Pollinations.",
                flush=True,
            )
            return False

        account_id = account["account_id"]
        token = account["token"]
        account_index = account["index"]
        excluded_accounts.add(account_id)

        api_url = (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{account_id}/ai/run/{CLOUDFLARE_MODEL}"
        )
        payload = {"prompt": prompt, "steps": CLOUDFLARE_STEPS}

        prompt_log_path = _log_final_api_prompt(
            prompt, provider=f"cloudflare_account_{account_index}"
        )
        print(f"📝 Final Cloudflare API prompt logged: {prompt_log_path}", flush=True)
        print(f"☁️ Trying Cloudflare account {account_index} ({account_id})", flush=True)
        print(
            f"📊 Account {account_index}: tracked neurons={account['neurons_used']:.2f}",
            flush=True,
        )

        account_success = False
        reservation_released = False
        quota_exhausted = False

        # Retry transient errors on the SAME account, but NEVER retry a quota
        # exhausted account.  A 429/4006 immediately moves to the next account.
        for attempt in range(1, CLOUDFLARE_RETRIES + 1):
            attempts_total += 1
            _USAGE["cloudflare_attempts"] += 1

            print(
                f"☁️ Cloudflare account {account_index} attempt "
                f"{attempt}/{CLOUDFLARE_RETRIES}",
                flush=True,
            )

            try:
                response = requests.post(
                    api_url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=CLOUDFLARE_TIMEOUT,
                )
            except requests.RequestException as exc:
                print(f"⚠️ Cloudflare account {account_index} request failed: {exc}", flush=True)
                if attempt < CLOUDFLARE_RETRIES:
                    time.sleep(2)
                    continue
                break

            try:
                error_data = response.json()
            except Exception:
                error_data = response.text[:2000]

            if response.status_code == 200:
                try:
                    _save_cloudflare_image(response, path)
                    response_data = error_data
                    reported = _extract_reported_neurons(response_data)
                    neurons = reported if reported is not None else estimated
                    source = "reported" if reported is not None else "estimated"

                    settle_reserved_neurons(account_id, estimated, neurons)
                    _record_image_usage(
                        path, "cloudflare", neurons, source, attempt,
                        final_prompt=prompt,
                    )
                    record_image(
                        account_id, neurons, source,
                        story_id=story_id, part_no=part_no,
                    )

                    print(
                        f"✅ Cloudflare account {account_index} SUCCESS; "
                        f"neurons={neurons:.2f}",
                        flush=True,
                    )
                    account_success = True
                    break
                except Exception as exc:
                    print(
                        f"⚠️ Cloudflare account {account_index} returned an "
                        f"invalid/unusable image: {exc}",
                        flush=True,
                    )
                    break

            # --------------------------------------------------------
            # DEFINITIVE DAILY QUOTA EXHAUSTION
            # --------------------------------------------------------
            error_text = json.dumps(error_data, ensure_ascii=False).lower()
            is_quota_exhausted = (
                "4006" in error_text
                or "daily free allocation" in error_text
                or "used up your daily free allocation" in error_text
                or "daily allocation" in error_text
                or ("neurons" in error_text and "used up" in error_text)
            )

            if response.status_code == 429 or is_quota_exhausted:
                print(
                    f"⚠️ Cloudflare account {account_index} API error "
                    f"(HTTP {response.status_code}): {error_data}",
                    flush=True,
                )

                if is_quota_exhausted:
                    quota_exhausted = True
                    # Undo only this request's reservation, then permanently
                    # skip this account for the rest of today's UTC bucket.
                    release_reserved_neurons(account_id, estimated)
                    reservation_released = True

                    mark_account_exhausted(
                        account_id,
                        reason="CLOUDFLARE_429_4006",
                    )

                    print(
                        f"🚫 Account {account_index} exhausted for UTC "
                        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}; "
                        "moving immediately to next account.",
                        flush=True,
                    )
                    break

                # Other 429/rate limiting: try the next account rather than
                # repeatedly spending time on the same account.
                print(
                    f"🔁 Account {account_index} returned non-quota 429; "
                    "moving to next account.",
                    flush=True,
                )
                break

            # --------------------------------------------------------
            # OTHER ACCOUNT-LEVEL ERRORS
            # --------------------------------------------------------
            print(
                f"⚠️ Cloudflare account {account_index} API error "
                f"(HTTP {response.status_code}): {error_data}",
                flush=True,
            )

            if response.status_code in (400, 401, 403, 500, 502, 503, 504):
                break

            if attempt < CLOUDFLARE_RETRIES:
                time.sleep(2)

        if account_success:
            return True

        if not reservation_released:
            release_reserved_neurons(account_id, estimated)

        if quota_exhausted:
            print(
                f"➡️ Quota-exhausted account {account_index} will NOT be "
                "called again today. Checking next account.",
                flush=True,
            )
        else:
            print(
                f"➡️ Cloudflare account {account_index} failed; "
                "checking next configured account.",
                flush=True,
            )

        # Continue until choose_account() has no remaining eligible accounts.


def _generate_pollinations(prompt, path):
    """Generate an image using Pollinations as the AI fallback."""
    url = (
        "https://image.pollinations.ai/prompt/"
        + urllib.parse.quote(prompt)
    )

    for attempt in range(1, POLLINATIONS_RETRIES + 1):
        try:
            print(
                f"🔄 Pollinations fallback attempt "
                f"{attempt}/{POLLINATIONS_RETRIES}",
                flush=True,
            )

            response = requests.get(
                url,
                timeout=POLLINATIONS_TIMEOUT,
            )

            if response.status_code == 200 and response.content:
                # Validate that the response is actually an image.
                image = Image.open(
                    BytesIO(response.content)
                )
                image.verify()

                with open(path, "wb") as f:
                    f.write(response.content)

                _record_image_usage(
                    path,
                    "pollinations",
                    0.0,
                    "not_applicable",
                    0,
                    final_prompt=prompt,
                )

                print(
                    f"✅ Pollinations fallback image saved: {path}",
                    flush=True,
                )
                print(
                    "   📊 Cloudflare neurons for this image: 0.00 "
                    "(Pollinations fallback)",
                    flush=True,
                )
                print(
                    f"   📊 Story Cloudflare neurons so far: "
                    f"{get_usage_summary()['total_cloudflare_neurons']:.2f}",
                    flush=True,
                )

                return True

            print(
                f"⚠️ Pollinations returned HTTP "
                f"{response.status_code}",
                flush=True,
            )

        except Exception as e:
            print(
                "⚠️ Pollinations fallback failed:",
                e,
                flush=True,
            )

        if attempt < POLLINATIONS_RETRIES:
            time.sleep(3)

    return False


# ============================================================
# PUBLIC IMAGE GENERATOR
# ============================================================

def generate_image(
    prompt,
    path,
    fallback_text=None,
    story_id=None,
    part_no=None,
):
    """
    Generate a scene image.

    Provider order:
        1. Cloudflare Workers AI
        2. Pollinations
        3. Local text fallback

    The rest of the application only needs to call this function.
    """

    prompt, was_trimmed = _trim_prompt(prompt)
    if was_trimmed:
        print(
            f"✂️ Image prompt trimmed to {CLOUDFLARE_MAX_PROMPT_CHARS} characters "
            "before provider selection.",
            flush=True,
        )

    # --------------------------------------------------------
    # 1. MULTI-ACCOUNT CLOUDFLARE ROUTING
    # --------------------------------------------------------

    if _generate_cloudflare(prompt, path, story_id=story_id, part_no=part_no):
        return path
    print(
        "⚠️ Cloudflare accounts unavailable/failed; switching to Pollinations fallback...",
        flush=True,
    )

    # --------------------------------------------------------
    # 2. POLLINATIONS AI FALLBACK
    # --------------------------------------------------------

    print(
        "🔄 Pollinations image generation...",
        flush=True,
    )

    if _generate_pollinations(prompt, path):
        return path

    # --------------------------------------------------------
    # 3. LOCAL FALLBACK
    # --------------------------------------------------------

    print(
        "🖼️ Using local fallback image...",
        flush=True,
    )

    try:
        img = Image.new(
            "RGB",
            VIDEO_SIZE,
            (20, 20, 20),
        )

        draw = ImageDraw.Draw(img)

        font = get_unicode_font(
            45,
            bold=True,
        )

        text = fallback_text or "Scene"

        words = text.split()
        lines = []
        line = ""

        for word in words:
            if len(line + word) < 20:
                line += word + " "
            else:
                lines.append(line.strip())
                line = word + " "

        if line.strip():
            lines.append(line.strip())

        lines = lines[:4]

        y = VIDEO_SIZE[1] // 2 - 100

        for i, line_text in enumerate(lines):
            bbox = draw.textbbox(
                (0, 0),
                line_text,
                font=font,
            )

            width = bbox[2] - bbox[0]

            draw.text(
                (
                    (VIDEO_SIZE[0] - width) // 2,
                    y + i * 60,
                ),
                line_text,
                font=font,
                fill=(255, 255, 255),
            )

        img.save(path)

        _record_image_usage(
            path,
            "local",
            0.0,
            "not_applicable",
            0,
        )

        print(
            f"⚠️ Local fallback saved: {path}",
            flush=True,
        )
        print(
            "   📊 Cloudflare neurons for this image: 0.00 "
            "(local fallback)",
            flush=True,
        )

        return path

    except Exception as e:
        print(
            "❌ Final fallback failed:",
            e,
            flush=True,
        )

        return None
