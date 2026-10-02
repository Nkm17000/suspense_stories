"""AI image generation with Cloudflare Workers AI as primary and Pollinations as fallback."""

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


# ============================================================
# CONFIGURATION
# ============================================================

CLOUDFLARE_ACCOUNT_ID = os.getenv(
    "CLOUDFLARE_ACCOUNT_ID", ""
).strip()

CLOUDFLARE_API_TOKEN = os.getenv(
    "CLOUDFLARE_API_TOKEN", ""
).strip()

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

# Cloudflare currently provides a 10,000-neuron daily free allocation.
# The REST inference response does not expose a documented daily-remaining
# counter, so this project keeps a persistent UTC-day ledger. If an API
# response ever contains an explicit neuron count, that value is used.
CLOUDFLARE_DAILY_NEURON_LIMIT = float(
    os.getenv("CLOUDFLARE_DAILY_NEURON_LIMIT", "10000")
)
CLOUDFLARE_POLLINATIONS_THRESHOLD = float(
    os.getenv("CLOUDFLARE_POLLINATIONS_THRESHOLD", "200")
)
DAILY_USAGE_DIR = os.getenv("CLOUDFLARE_DAILY_USAGE_DIR", "logs")

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


def _utc_day_key():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _daily_usage_path(day=None):
    day = day or _utc_day_key()
    os.makedirs(DAILY_USAGE_DIR, exist_ok=True)
    return os.path.join(
        DAILY_USAGE_DIR,
        f"cloudflare_daily_usage_{day}.json",
    )


def _load_daily_usage(day=None):
    day = day or _utc_day_key()
    path = _daily_usage_path(day)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date_utc") == day:
                return data
        except Exception:
            pass
    return {
        "date_utc": day,
        "daily_limit_neurons": CLOUDFLARE_DAILY_NEURON_LIMIT,
        "cloudflare_neurons_used": 0.0,
        "cloudflare_images": 0,
        "estimated_neurons": 0.0,
        "reported_neurons": 0.0,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "note": "Local ledger. Cloudflare dashboard is authoritative.",
    }


def _save_daily_usage(data):
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    path = _daily_usage_path(data.get("date_utc"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def get_daily_cloudflare_status():
    """Return the persisted UTC-day neuron ledger and remaining allowance."""
    data = _load_daily_usage()
    used = max(0.0, float(data.get("cloudflare_neurons_used", 0.0)))
    limit = max(0.0, float(data.get("daily_limit_neurons", CLOUDFLARE_DAILY_NEURON_LIMIT)))
    remaining = max(0.0, limit - used)
    return {
        **data,
        "cloudflare_neurons_remaining": round(remaining, 2),
        "pollinations_threshold": CLOUDFLARE_POLLINATIONS_THRESHOLD,
        "cloudflare_allowed_by_threshold": remaining > CLOUDFLARE_POLLINATIONS_THRESHOLD,
        "usage_file": _daily_usage_path(data.get("date_utc")),
    }


def _record_daily_cloudflare_neurons(neurons, source):
    data = _load_daily_usage()
    value = max(0.0, float(neurons))
    data["cloudflare_neurons_used"] = round(
        float(data.get("cloudflare_neurons_used", 0.0)) + value,
        4,
    )
    data["cloudflare_images"] = int(data.get("cloudflare_images", 0)) + 1
    if source == "reported":
        data["reported_neurons"] = round(
            float(data.get("reported_neurons", 0.0)) + value,
            4,
        )
    else:
        data["estimated_neurons"] = round(
            float(data.get("estimated_neurons", 0.0)) + value,
            4,
        )
    return _save_daily_usage(data)


def _print_daily_cloudflare_status(prefix="📅 Cloudflare daily status"):
    status = get_daily_cloudflare_status()
    print(
        f"{prefix} | UTC {status['date_utc']} | "
        f"used={status['cloudflare_neurons_used']:.2f} | "
        f"remaining={status['cloudflare_neurons_remaining']:.2f} | "
        f"threshold={CLOUDFLARE_POLLINATIONS_THRESHOLD:.2f}",
        flush=True,
    )
    return status


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


def _generate_cloudflare(prompt, path):
    """Generate an image using Cloudflare Workers AI."""
    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
        print(
            "ℹ️ Cloudflare credentials are not configured; "
            "skipping to Pollinations fallback.",
            flush=True,
        )
        return False

    api_url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{CLOUDFLARE_ACCOUNT_ID}/ai/run/"
        f"{CLOUDFLARE_MODEL}"
    )

    payload = {
        "prompt": prompt,
        "steps": CLOUDFLARE_STEPS,
    }

    prompt_log_path = _log_final_api_prompt(prompt, provider="cloudflare")
    print(
        f"📝 Final Cloudflare API prompt logged: {prompt_log_path}",
        flush=True,
    )
    print("📝 FINAL IMAGE PROMPT SENT TO CLOUDFLARE:", flush=True)
    print(f"   {prompt}", flush=True)

    for attempt in range(1, CLOUDFLARE_RETRIES + 1):
        _USAGE["cloudflare_attempts"] += 1
        try:
            print(
                f"☁️ Cloudflare image attempt "
                f"{attempt}/{CLOUDFLARE_RETRIES}",
                flush=True,
            )

            response = requests.post(
                api_url,
                headers={
                    "Authorization": (
                        f"Bearer {CLOUDFLARE_API_TOKEN}"
                    ),
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=CLOUDFLARE_TIMEOUT,
            )

            if response.status_code == 200:
                try:
                    _save_cloudflare_image(response, path)
                    response_data = response.json()
                    reported = _extract_reported_neurons(response_data)
                    neurons = (
                        reported
                        if reported is not None
                        else _estimated_cloudflare_neurons()
                    )
                    source = "reported" if reported is not None else "estimated"
                    _record_image_usage(
                        path,
                        "cloudflare",
                        neurons,
                        source,
                        attempt,
                        final_prompt=prompt,
                    )
                    _record_daily_cloudflare_neurons(neurons, source)

                    print(
                        f"✅ Cloudflare image saved: {path}",
                        flush=True,
                    )
                    print(
                        f"   📊 Cloudflare neurons for this image: {neurons:.2f} ({source})",
                        flush=True,
                    )
                    print(
                        f"   📊 Story Cloudflare neurons so far: "
                        f"{get_usage_summary()['total_cloudflare_neurons']:.2f}",
                        flush=True,
                    )
                    _print_daily_cloudflare_status()

                    return True

                except Exception as e:
                    print(
                        "⚠️ Cloudflare returned an invalid image:",
                        e,
                        flush=True,
                    )

            else:
                # Do not print the Authorization header/token.
                try:
                    error_data = response.json()
                except Exception:
                    error_data = response.text[:1000]

                print(
                    f"⚠️ Cloudflare API error "
                    f"(HTTP {response.status_code}): "
                    f"{error_data}",
                    flush=True,
                )

        except requests.RequestException as e:
            print(
                "⚠️ Cloudflare request failed:",
                e,
                flush=True,
            )

        if attempt < CLOUDFLARE_RETRIES:
            time.sleep(2)

    return False


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
):
    """
    Generate a scene image.

    Provider order:
        1. Cloudflare Workers AI
        2. Pollinations
        3. Local text fallback

    The rest of the application only needs to call this function.
    """

    # --------------------------------------------------------
    # 1. DAILY NEURON THRESHOLD ROUTING
    # --------------------------------------------------------

    print("📝 FINAL IMAGE PROMPT:", flush=True)
    print(f"   {prompt}", flush=True)

    daily_status = _print_daily_cloudflare_status(
        "📅 Before image provider selection"
    )

    if daily_status["cloudflare_neurons_remaining"] > CLOUDFLARE_POLLINATIONS_THRESHOLD:
        print(
            f"☁️ Remaining neurons {daily_status['cloudflare_neurons_remaining']:.2f} "
            f"> {CLOUDFLARE_POLLINATIONS_THRESHOLD:.2f}; trying Cloudflare.",
            flush=True,
        )
        if _generate_cloudflare(prompt, path):
            return path
        print(
            "⚠️ Cloudflare failed; switching to Pollinations fallback...",
            flush=True,
        )
    else:
        print(
            f"🔄 Remaining neurons {daily_status['cloudflare_neurons_remaining']:.2f} "
            f"<= {CLOUDFLARE_POLLINATIONS_THRESHOLD:.2f}; "
            "skipping Cloudflare and using Pollinations.",
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
