"""Reliable AI image generation with retries and safe fallbacks."""

import os
import time
import urllib.parse
from pathlib import Path

import requests
from PIL import Image, ImageDraw

from .config import VIDEO_SIZE
from .fonts import get_unicode_font


# ============================================================
# CONFIGURATION
# ============================================================

POLLINATIONS_URL = "https://image.pollinations.ai/prompt/"

# AI image generation can legitimately take longer than 20 seconds.
CONNECT_TIMEOUT = 20
READ_TIMEOUT = 90

# Number of Pollinations attempts.
MAX_POLLINATIONS_ATTEMPTS = 4

# Delay happens ONLY after a failed attempt.
# There is NO artificial delay before attempt 1.
RETRY_DELAYS = (
    3,
    7,
    12,
)

# Minimum valid response size.
# This prevents HTML/error pages being saved as PNG.
MIN_IMAGE_BYTES = 10 * 1024

# ============================================================
# HTTP SESSION
# ============================================================

def _create_session():
    """
    Create a reusable HTTP session.

    This avoids repeatedly creating a new TCP/TLS connection and
    gives the first request the same HTTP configuration as retries.
    """

    session = requests.Session()

    session.headers.update(
        {
            "User-Agent": (
                "SmartLearningLabVideoGenerator/1.0 "
                "(GitHub Actions)"
            ),
            "Accept": "image/avif,image/webp,image/png,"
            "image/jpeg,image/*;q=0.8",
            "Connection": "keep-alive",
        }
    )

    return session


# ============================================================
# IMAGE VALIDATION
# ============================================================

def _validate_image_bytes(content):
    """
    Verify that downloaded bytes are actually a readable image.

    Returns:
        True  -> valid image
        False -> invalid image
    """

    if not content:
        return False

    if len(content) < MIN_IMAGE_BYTES:
        return False

    try:
        from io import BytesIO

        with Image.open(
            BytesIO(content)
        ) as image:

            # Force PIL to actually decode the image.
            image.verify()

        return True

    except Exception as exc:

        print(
            f"⚠️ Downloaded response is not a valid image: {exc}",
            flush=True,
        )

        return False


# ============================================================
# ATOMIC IMAGE SAVE
# ============================================================

def _save_image_atomically(
    content,
    path,
):
    """
    Save the image to a temporary file first.

    This prevents partially downloaded files from being mistaken
    for successfully generated images.
    """

    target = Path(path)

    target.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp_path = target.with_suffix(
        target.suffix + ".tmp"
    )

    try:

        with open(
            temp_path,
            "wb",
        ) as file:

            file.write(content)

            file.flush()

            # Make sure Python/OS buffers are flushed before rename.
            os.fsync(
                file.fileno()
            )

        # Validate the actual temporary file.
        with Image.open(
            temp_path
        ) as image:

            image.verify()

        # Atomic replacement.
        os.replace(
            temp_path,
            target,
        )

        return True

    except Exception as exc:

        print(
            f"⚠️ Could not save image safely: {exc}",
            flush=True,
        )

        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass

        return False


# ============================================================
# POLLINATIONS IMAGE REQUEST
# ============================================================

def _request_pollinations_image(
    session,
    url,
):
    """
    Perform one complete Pollinations request.

    IMPORTANT:
    This function is identical for attempt 1, 2, 3 and 4.
    There is no special 'first attempt' path.
    """

    response = session.get(
        url,
        timeout=(
            CONNECT_TIMEOUT,
            READ_TIMEOUT,
        ),
        allow_redirects=True,
    )

    status = response.status_code

    print(
        f"   HTTP status: {status}",
        flush=True,
    )

    if status != 200:

        # Show useful information without dumping a huge response.
        content_type = response.headers.get(
            "Content-Type",
            "unknown",
        )

        print(
            f"⚠️ Pollinations returned HTTP {status} "
            f"(Content-Type: {content_type})",
            flush=True,
        )

        return None

    content_type = response.headers.get(
        "Content-Type",
        "",
    ).lower()

    print(
        f"   Content-Type: {content_type}",
        flush=True,
    )

    # If server explicitly says it isn't an image, reject it.
    if (
        content_type
        and not content_type.startswith("image/")
    ):

        print(
            "⚠️ Pollinations response is not an image.",
            flush=True,
        )

        return None

    content = response.content

    print(
        f"   Response size: "
        f"{len(content) / 1024:.1f} KB",
        flush=True,
    )

    if not _validate_image_bytes(
        content
    ):

        print(
            "⚠️ Pollinations returned invalid image data.",
            flush=True,
        )

        return None

    return content


# ============================================================
# IMAGE GENERATION
# ============================================================

def generate_image(
    prompt,
    path,
    fallback_text=None,
):
    """
    Generate an image reliably.

    Pipeline:

        Pollinations attempt 1
             ↓
        Pollinations attempt 2
             ↓
        Pollinations attempt 3
             ↓
        Pollinations attempt 4
             ↓
        Picsum
             ↓
        Dummy image
             ↓
        Local generated fallback

    Every Pollinations attempt uses exactly the same request logic.
    """

    if not prompt:
        raise ValueError(
            "Image generation prompt cannot be empty."
        )

    target_path = Path(path)

    target_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------------
    # Normalize prompt
    # --------------------------------------------------------

    prompt = str(prompt).strip()

    # --------------------------------------------------------
    # Pollinations URL
    # --------------------------------------------------------

    encoded_prompt = urllib.parse.quote(
        prompt,
        safe="",
    )

    url = (
        POLLINATIONS_URL
        + encoded_prompt
    )

    print(
        "\n🖼️ Starting image generation",
        flush=True,
    )

    print(
        f"   Output: {path}",
        flush=True,
    )

    print(
        f"   Prompt length: {len(prompt)} characters",
        flush=True,
    )

    # --------------------------------------------------------
    # Reusable HTTP session.
    # --------------------------------------------------------

    session = _create_session()

    try:

        # ====================================================
        # 1. POLLINATIONS
        # ====================================================

        for attempt in range(
            1,
            MAX_POLLINATIONS_ATTEMPTS + 1,
        ):

            print(
                "\n"
                + "=" * 50,
                flush=True,
            )

            print(
                f"🖼️ Pollinations attempt "
                f"{attempt}/{MAX_POLLINATIONS_ATTEMPTS}",
                flush=True,
            )

            print(
                "   🚀 Sending image request...",
                flush=True,
            )

            # IMPORTANT:
            # NO sleep before attempt 1.
            #
            # Every attempt executes exactly the same request.
            try:

                start_time = time.time()

                content = _request_pollinations_image(
                    session,
                    url,
                )

                elapsed = (
                    time.time()
                    - start_time
                )

                print(
                    f"   ⏱️ Request time: "
                    f"{elapsed:.2f}s",
                    flush=True,
                )

                if content:

                    print(
                        "   ✅ Valid image received.",
                        flush=True,
                    )

                    if _save_image_atomically(
                        content,
                        path,
                    ):

                        print(
                            f"   ✅ Image saved: {path}",
                            flush=True,
                        )

                        return path

                    print(
                        "   ⚠️ Image save failed.",
                        flush=True,
                    )

            except requests.exceptions.Timeout as exc:

                print(
                    f"⚠️ Pollinations timeout: {exc}",
                    flush=True,
                )

            except requests.exceptions.ConnectionError as exc:

                print(
                    f"⚠️ Pollinations connection error: {exc}",
                    flush=True,
                )

            except requests.exceptions.RequestException as exc:

                print(
                    f"⚠️ Pollinations request error: {exc}",
                    flush=True,
                )

            except Exception as exc:

                print(
                    f"⚠️ Pollinations unexpected error: {exc}",
                    flush=True,
                )

            # ------------------------------------------------
            # Retry delay.
            #
            # IMPORTANT:
            # Delay happens AFTER a failure, never before
            # attempt 1.
            # ------------------------------------------------

            if attempt < MAX_POLLINATIONS_ATTEMPTS:

                delay_index = attempt - 1

                delay = RETRY_DELAYS[
                    min(
                        delay_index,
                        len(RETRY_DELAYS) - 1,
                    )
                ]

                print(
                    f"⏳ Waiting {delay}s before "
                    f"Pollinations attempt "
                    f"{attempt + 1}...",
                    flush=True,
                )

                time.sleep(
                    delay
                )

    finally:

        try:
            session.close()
        except Exception:
            pass

    # ========================================================
    # 2. PICSUM FALLBACK
    # ========================================================

    print(
        "\n🖼️ Pollinations unavailable after "
        f"{MAX_POLLINATIONS_ATTEMPTS} attempts.",
        flush=True,
    )

    print(
        "🖼️ Trying Picsum fallback...",
        flush=True,
    )

    try:

        picsum_url = (
            "https://picsum.photos/720/1280"
            f"?random={int(time.time() * 1000)}"
        )

        response = requests.get(
            picsum_url,
            headers={
                "User-Agent": (
                    "SmartLearningLabVideoGenerator/1.0"
                )
            },
            timeout=(
                15,
                30,
            ),
        )

        print(
            f"   Picsum HTTP status: "
            f"{response.status_code}",
            flush=True,
        )

        if (
            response.status_code == 200
            and _validate_image_bytes(
                response.content
            )
        ):

            if _save_image_atomically(
                response.content,
                path,
            ):

                print(
                    f"✅ Picsum image saved: {path}",
                    flush=True,
                )

                return path

    except Exception as exc:

        print(
            f"⚠️ Picsum failed: {exc}",
            flush=True,
        )

    # ========================================================
    # 3. DUMMY IMAGE FALLBACK
    # ========================================================

    print(
        "\n🖼️ Trying dummy image fallback...",
        flush=True,
    )

    try:

        text = (
            fallback_text
            or "Scene"
        )

        dummy_url = (
            "https://dummyimage.com/720x1280/000/fff"
            "?text="
            + urllib.parse.quote(
                str(text)[:80],
                safe="",
            )
        )

        response = requests.get(
            dummy_url,
            headers={
                "User-Agent": (
                    "SmartLearningLabVideoGenerator/1.0"
                )
            },
            timeout=(
                10,
                20,
            ),
        )

        print(
            f"   Dummy HTTP status: "
            f"{response.status_code}",
            flush=True,
        )

        if (
            response.status_code == 200
            and _validate_image_bytes(
                response.content
            )
        ):

            if _save_image_atomically(
                response.content,
                path,
            ):

                print(
                    f"✅ Dummy image saved: {path}",
                    flush=True,
                )

                return path

    except Exception as exc:

        print(
            f"⚠️ Dummy image failed: {exc}",
            flush=True,
        )

    # ========================================================
    # 4. LOCAL FALLBACK
    # ========================================================

    print(
        "\n🖼️ Creating local fallback image...",
        flush=True,
    )

    try:

        width, height = VIDEO_SIZE

        img = Image.new(
            "RGB",
            VIDEO_SIZE,
            (20, 20, 20),
        )

        draw = ImageDraw.Draw(
            img
        )

        font = get_unicode_font(
            45,
            bold=True,
        )

        text = str(
            fallback_text
            or "Scene"
        )

        words = text.split()

        lines = []

        line = ""

        # ----------------------------------------------------
        # Hindi + English friendly wrapping.
        #
        # Do NOT use len(line + word) as the only condition
        # because Hindi glyphs do not have the same visual width
        # as Latin characters.
        # ----------------------------------------------------

        max_chars = 24

        for word in words:

            candidate = (
                f"{line} {word}"
                if line
                else word
            )

            if len(candidate) <= max_chars:

                line = candidate

            else:

                if line:
                    lines.append(
                        line
                    )

                line = word

        if line:
            lines.append(
                line
            )

        lines = lines[:5]

        line_height = 65

        total_height = (
            len(lines)
            * line_height
        )

        y = (
            height
            - total_height
        ) // 2

        for line_text in lines:

            bbox = draw.textbbox(
                (0, 0),
                line_text,
                font=font,
                stroke_width=1,
            )

            text_width = (
                bbox[2]
                - bbox[0]
            )

            x = (
                width
                - text_width
            ) // 2

            draw.text(
                (
                    x,
                    y,
                ),
                line_text,
                font=font,
                fill=(255, 255, 255),
                stroke_width=1,
                stroke_fill=(0, 0, 0),
            )

            y += line_height

        img.save(
            path,
            format="PNG",
        )

        if (
            os.path.isfile(path)
            and os.path.getsize(path) > 0
        ):

            print(
                f"✅ Local fallback saved: {path}",
                flush=True,
            )

            return path

    except Exception as exc:

        print(
            f"❌ Final local fallback failed: {exc}",
            flush=True,
        )

    # ========================================================
    # COMPLETE FAILURE
    # ========================================================

    print(
        f"❌ Image generation completely failed: {path}",
        flush=True,
    )

    return None