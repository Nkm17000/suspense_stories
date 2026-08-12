"""Opening title card using the supplied poster design.

Only the title is dynamic. The title is read from the MongoDB story document.
Place the supplied poster artwork at assets/title_page_template.png or set
TITLE_TEMPLATE_PATH.
"""

import os
import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from moviepy.editor import ImageClip

from .config import VIDEO_SIZE, TITLE_TEMPLATE_PATH, TITLE_TEMPLATE_CROP, TITLE_CARD_DURATION
from .fonts import find_devanagari_font
from .voice import clean_tts_text
from .branding import prepare_round_logo

def _load_title_template():
    """
    Load the exact poster supplied by the user.

    The supplied screenshot contains an outer white border and some
    surrounding screenshot area. We crop it to the actual 9:16 poster
    and then resize it to VIDEO_SIZE.
    """
    if not os.path.exists(TITLE_TEMPLATE_PATH):
        print(
            f"⚠️ Title template not found: {TITLE_TEMPLATE_PATH}",
            flush=True
        )
        return None

    try:
        source = Image.open(TITLE_TEMPLATE_PATH).convert("RGB")

        w, h = source.size

        # Crop the outer white/screenshot border using normalized values.
        left = int(w * TITLE_TEMPLATE_CROP[0])
        top = int(h * TITLE_TEMPLATE_CROP[1])
        right = int(w * TITLE_TEMPLATE_CROP[2])
        bottom = int(h * TITLE_TEMPLATE_CROP[3])

        if right <= left or bottom <= top:
            raise ValueError("Invalid title template crop")

        source = source.crop((left, top, right, bottom))

        source = source.resize(
            VIDEO_SIZE,
            Image.Resampling.LANCZOS
        )

        return source

    except Exception as e:
        print(
            f"⚠️ Could not load title template: {e}",
            flush=True
        )
        return None


def _cover_existing_title(img):
    """
    Remove the hard-coded title from the supplied poster.

    The title region is replaced across the full width with a smooth
    background gradient sampled from the original artwork. Because the
    replacement spans the full width, there is no visible rectangular
    "box" behind the dynamic title.
    """
    width, height = VIDEO_SIZE
    pixels = np.array(img).astype(np.float32)

    # Only the old title area is replaced.
    # The logo, top ornament, subtitle plaque, mountains, wolf, hunter,
    # book and bottom glow remain exactly from the supplied image.
    y1 = int(height * 0.250)
    y2 = int(height * 0.555)

    replacement = pixels.copy()

    def row_background(y):
        # Avoid the original title in the center and sample the real
        # background from the left/right edges.
        left = pixels[y, :100]
        right = pixels[y, width - 100:]

        return np.median(
            np.concatenate([left, right], axis=0),
            axis=0
        )

    top_color = row_background(y1)
    bottom_color = row_background(y2)

    for y in range(y1, y2):
        progress = (y - y1) / max(1, y2 - y1)

        # Smooth vertical color transition.
        color = (
            top_color * (1.0 - progress)
            + bottom_color * progress
        )

        replacement[y, :] = color

    replacement = Image.fromarray(
        np.uint8(np.clip(replacement, 0, 255)),
        "RGB"
    )

    # Very small feather only at the top/bottom so the gradient blends
    # naturally into the original poster.
    mask = Image.new("L", (width, height), 0)

    mask_draw = ImageDraw.Draw(mask)

    mask_draw.rectangle(
        (0, y1, width, y2),
        fill=255
    )

    mask = mask.filter(
        ImageFilter.GaussianBlur(2)
    )

    result = Image.composite(
        replacement,
        img,
        mask
    )

    # Add a subtle warm glow in the center of the title area.
    yy, xx = np.ogrid[:height, :width]

    cx = width // 2
    cy = int(height * 0.47)

    distance = np.sqrt(
        ((xx - cx) / (width * 0.48)) ** 2
        + ((yy - cy) / (height * 0.22)) ** 2
    )

    alpha = np.clip(1.0 - distance, 0, 1) ** 4

    glow_mask = Image.fromarray(
        (alpha * 22).astype(np.uint8),
        "L"
    )

    gold_layer = Image.new(
        "RGBA",
        (width, height),
        (255, 150, 20, 0)
    )

    gold_layer.putalpha(glow_mask)

    result = Image.alpha_composite(
        result.convert("RGBA"),
        gold_layer
    ).convert("RGB")

    return result


def _fit_font_for_text(
    text,
    max_width,
    start_size,
    bold=True,
    max_height=None,
    min_size=28
):
    """
    Return the largest Devanagari font that fits both width and height.

    This is important for long Hindi titles. The old implementation only
    checked width, which could still produce an oversized line vertically
    and cause the title to collide with other poster elements.
    """
    if not text:
        return find_devanagari_font(min_size, bold=bold)

    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))

    for size in range(start_size, min_size - 1, -2):
        font = find_devanagari_font(size, bold=bold)

        bbox = probe.textbbox(
            (0, 0),
            text,
            font=font,
            stroke_width=2
        )

        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        width_ok = text_width <= max_width
        height_ok = (
            max_height is None
            or text_height <= max_height
        )

        if width_ok and height_ok:
            return font

    return find_devanagari_font(min_size, bold=bold)


def _draw_3d_title(
    draw,
    center_x,
    y,
    text,
    font,
    fill,
    align="center",
):
    """
    Recreate the heavy 3D/shadow look of the supplied title artwork.
    """
    bbox = draw.textbbox(
        (0, 0),
        text,
        font=font,
        stroke_width=2
    )

    text_width = bbox[2] - bbox[0]

    # Horizontal alignment for special 2-word titles.
    if align == "left":
        x = int(draw._image.width * 0.08)
    elif align == "right":
        x = int(draw._image.width * 0.92) - text_width
    else:
        x = center_x - text_width // 2

    # Deep black/brown shadow.
    for offset in range(16, 5, -2):
        draw.text(
            (x + 2, y + offset),
            text,
            font=font,
            fill=(25, 18, 12),
            stroke_width=5,
            stroke_fill=(8, 12, 18)
        )

    # Dark extrusion edge.
    draw.text(
        (x, y + 5),
        text,
        font=font,
        fill=fill,
        stroke_width=5,
        stroke_fill=(55, 30, 8)
    )

    # Main face.
    draw.text(
        (x, y),
        text,
        font=font,
        fill=fill,
        stroke_width=2,
        stroke_fill=(255, 235, 190) if fill[0] > 200 else (20, 20, 20)
    )

    # Small top highlight.
    draw.text(
        (x, y - 2),
        text,
        font=font,
        fill=fill,
        stroke_width=1,
        stroke_fill=(255, 255, 255) if fill[0] > 200 else (120, 55, 0)
    )


def _normalize_title(title):
    """
    Clean a title before rendering.

    Protects the title card from duplicated titles coming from MongoDB
    or generated story data.

    Examples:
        "A B C A B C" -> "A B C"
        "A B A B C"   -> "A B C"
    """
    title = clean_tts_text(title or "").strip()

    if not title:
        return "Untitled Story"

    words = title.split()

    # Remove exact duplicated halves:
    # "लोमड़ी और लड़की ... लोमड़ी और लड़की ..."
    if len(words) >= 4 and len(words) % 2 == 0:
        half = len(words) // 2
        if words[:half] == words[half:]:
            words = words[:half]

    # Remove immediate repeated phrase at the beginning:
    # "A B C A B C D" -> "A B C D"
    changed = True
    while changed and len(words) >= 4:
        changed = False

        for phrase_len in range(
            len(words) // 2,
            1,
            -1,
        ):
            if words[:phrase_len] == words[phrase_len:2 * phrase_len]:
                words = words[phrase_len:]
                changed = True
                break

    return " ".join(words).strip()


def _split_title_for_design(title):
    """
    Split the title according to the requested word-count rules.

    Rules:
      - Fewer than 6 words:
          Keep the existing logic.
          If "और" exists, render:
              first phrase / और / remaining phrase.
          Otherwise use the existing balanced split.

      - 6 to 12 words:
          Line 1 = first 2 words
          Line 2 = next 3 words
          Line 3 = all remaining words

      - More than 12 words:
          Divide into 3 nearly equal groups.
          Extra words ALWAYS go to the last line.

          Examples:
              13 -> 4 / 4 / 5
              14 -> 4 / 4 / 6
              15 -> 5 / 5 / 5
              16 -> 5 / 5 / 6
              17 -> 5 / 5 / 7

      - Exactly 2 words:
          Line 1 = first word, LEFT aligned
          Line 3 = second word, RIGHT aligned

    IMPORTANT:
      The function does not remove or change words from the title.
      It only controls how the title is displayed.
    """

    title = _normalize_title(title)
    words = title.split()
    count = len(words)

    # --------------------------------------------------------
    # Exactly 2 words
    # --------------------------------------------------------
    if count == 2:
        return (
            words[0],
            "",
            words[1],
            "left",
            "right",
        )

    # --------------------------------------------------------
    # Fewer than 6 words: keep the existing logic
    # --------------------------------------------------------
    if count < 6:

        if "और" in title:
            before, after = title.split("और", 1)

            return (
                before.strip(),
                "और",
                after.strip(),
                "center",
                "center",
            )

        if count <= 3:
            return (
                title,
                "",
                "",
                "center",
                "center",
            )

        midpoint = max(1, count // 2)

        return (
            " ".join(words[:midpoint]),
            "",
            " ".join(words[midpoint:]),
            "center",
            "center",
        )

    # --------------------------------------------------------
    # 6 to 12 words
    #
    # 1st line = 2 words
    # 2nd line = 3 words
    # 3rd line = remaining words
    # --------------------------------------------------------
    if 6 <= count <= 12:

        return (
            " ".join(words[:2]),
            " ".join(words[2:5]),
            " ".join(words[5:]),
            "center",
            "center",
        )

    # --------------------------------------------------------
    # More than 12 words
    #
    # Divide by 3.
    # Any remainder goes ONLY to the last line.
    #
    # Examples:
    # 13 = 4 / 4 / 5
    # 14 = 4 / 4 / 6
    # 15 = 5 / 5 / 5
    # --------------------------------------------------------
    base = count // 3

    first_count = base
    second_count = base
    last_count = count - first_count - second_count

    return (
        " ".join(words[:first_count]),
        " ".join(
            words[first_count:first_count + second_count]
        ),
        " ".join(
            words[first_count + second_count:]
        ),
        "center",
        "center",
    )

def create_title_card(title, duration=TITLE_CARD_DURATION, part_no=None):
    """
    Create the opening title page using the EXACT supplied poster artwork.

    Only the story title is dynamic and comes from MongoDB.
    Everything else remains based on the supplied design.
    """
    original_title = clean_tts_text(title) or "Untitled Story"
    if part_no is not None:
        part_no = int(part_no)
    title = _normalize_title(original_title)

    if title != original_title:
        print(
            "⚠️ Duplicate/repeated title detected.",
            flush=True,
        )
        print(
            f"   Original: {original_title}",
            flush=True,
        )
        print(
            f"   Rendered: {title}",
            flush=True,
        )

    print(
        f"📖 Creating supplied-design opening title page: {title}",
        flush=True
    )

    template = _load_title_template()

    if template is None:
        # Safe fallback if the template image was not copied into the repo.
        # This keeps the pipeline working instead of crashing.
        print(
            "⚠️ Falling back to generated title card because "
            "the supplied template image is missing.",
            flush=True
        )

        img = Image.new("RGB", VIDEO_SIZE, (7, 26, 51))
        draw = ImageDraw.Draw(img)

        for y in range(VIDEO_SIZE[1]):
            ratio = y / max(1, VIDEO_SIZE[1] - 1)
            draw.line(
                (0, y, VIDEO_SIZE[0], y),
                fill=(
                    int(7 + 4 * ratio),
                    int(26 + 18 * ratio),
                    int(51 + 20 * ratio)
                )
            )

        logo_path = prepare_round_logo()

        if logo_path and os.path.exists(logo_path):
            with Image.open(logo_path).convert("RGBA") as logo:
                logo = logo.resize(
                    (190, 190),
                    Image.Resampling.LANCZOS
                )
                img.paste(
                    logo,
                    ((VIDEO_SIZE[0] - 190) // 2, 85),
                    logo
                )
    else:
        img = _cover_existing_title(template)

    draw = ImageDraw.Draw(img)

    width, height = VIDEO_SIZE
    center_x = width // 2

    # ------------------------------------------------------------
    # Dynamic DB title
    # ------------------------------------------------------------
    (
        first,
        middle,
        last,
        first_align,
        last_align,
    ) = _split_title_for_design(title)

    print(
        "📝 TITLE LAYOUT:",
        flush=True,
    )
    print(
        f"   Line 1 ({len(first.split()) if first else 0} words): {first}",
        flush=True,
    )
    print(
        f"   Line 2 ({len(middle.split()) if middle else 0} words): {middle}",
        flush=True,
    )
    print(
        f"   Line 3 ({len(last.split()) if last else 0} words): {last}",
        flush=True,
    )

    # The title has a fixed safe region between the top artwork and the
    # subtitle plaque. Font size is calculated from BOTH available width
    # and available vertical space, so long titles automatically become
    # smaller instead of overflowing/repeating across the card.
    title_top = int(height * 0.255)
    title_bottom = int(height * 0.555)
    title_region_height = title_bottom - title_top

    # Small horizontal safety margin.
    title_max_width = int(width * 0.86)

    if middle:
        # Three visual parts.
        # Allocate vertical space across all three title lines.
        first_max_height = int(title_region_height * 0.32)
        middle_max_height = int(title_region_height * 0.22)
        last_max_height = int(title_region_height * 0.32)

        first_font = _fit_font_for_text(
            first,
            title_max_width,
            125,
            bold=True,
            max_height=first_max_height,
            min_size=30
        )

        middle_font = _fit_font_for_text(
            middle,
            int(width * 0.35),
            72,
            bold=True,
            max_height=middle_max_height,
            min_size=28
        )

        last_font = _fit_font_for_text(
            last,
            title_max_width,
            125,
            bold=True,
            max_height=last_max_height,
            min_size=30
        )

        # Calculate actual rendered heights and center each line inside
        # its allocated slot. This prevents long titles from overlapping.
        def text_height(font, value, stroke=2):
            bbox = draw.textbbox(
                (0, 0),
                value,
                font=font,
                stroke_width=stroke
            )
            return bbox[3] - bbox[1]

        first_h = text_height(first_font, first)
        middle_h = text_height(middle_font, middle)
        last_h = text_height(last_font, last)

        first_slot_top = int(height * 0.265)
        first_slot_bottom = int(height * 0.355)

        middle_slot_top = int(height * 0.355)
        middle_slot_bottom = int(height * 0.425)

        last_slot_top = int(height * 0.425)
        last_slot_bottom = int(height * 0.515)

        first_y = first_slot_top + max(
            0,
            (first_slot_bottom - first_slot_top - first_h) // 2
        )

        middle_y = middle_slot_top + max(
            0,
            (middle_slot_bottom - middle_slot_top - middle_h) // 2
        )

        last_y = last_slot_top + max(
            0,
            (last_slot_bottom - last_slot_top - last_h) // 2
        )

        _draw_3d_title(
            draw,
            center_x,
            first_y,
            first,
            first_font,
            (255, 143, 10),
            align=first_align,
        )

        _draw_3d_title(
            draw,
            center_x,
            middle_y,
            middle,
            middle_font,
            (255, 143, 10)
        )

        _draw_3d_title(
            draw,
            center_x,
            last_y,
            last,
            last_font,
            (255, 157, 12),
            align=last_align,
        )

    else:
        # Titles without "और" are split into two balanced phrases.
        # Use a slightly smaller maximum size because the entire title
        # must fit comfortably inside the same safe region.
        first_font = _fit_font_for_text(
            first,
            title_max_width,
            120,
            bold=True,
            max_height=int(title_region_height * 0.38),
            min_size=30
        )

        last_font = None

        if last:
            last_font = _fit_font_for_text(
                last,
                title_max_width,
                120,
                bold=True,
                max_height=int(title_region_height * 0.38),
                min_size=30
            )

        def text_height(font, value, stroke=2):
            bbox = draw.textbbox(
                (0, 0),
                value,
                font=font,
                stroke_width=stroke
            )
            return bbox[3] - bbox[1]

        first_h = text_height(first_font, first)

        if last:
            last_h = text_height(last_font, last)

            first_slot_top = int(height * 0.285)
            first_slot_bottom = int(height * 0.395)

            last_slot_top = int(height * 0.395)
            last_slot_bottom = int(height * 0.505)

            first_y = first_slot_top + max(
                0,
                (first_slot_bottom - first_slot_top - first_h) // 2
            )

            last_y = last_slot_top + max(
                0,
                (last_slot_bottom - last_slot_top - last_h) // 2
            )

            _draw_3d_title(
                draw,
                center_x,
                first_y,
                first,
                first_font,
                (255, 143, 10),
                align=first_align,
            )

            _draw_3d_title(
                draw,
                center_x,
                last_y,
                last,
                last_font,
                (255, 157, 12),
                align=last_align,
            )

        else:
            # Very short one-line title.
            first_y = int(height * 0.355)

            _draw_3d_title(
                draw,
                center_x,
                first_y,
                first,
                first_font,
                (255, 143, 10),
                align=first_align,
            )

    # ------------------------------------------------------------
    # Dynamic part number
    # ------------------------------------------------------------
    if part_no is not None:
        part_text = f"भाग {part_no}"
        part_font = _fit_font_for_text(
            part_text,
            int(width * 0.42),
            58,
            bold=True,
            max_height=int(height * 0.055),
            min_size=28,
        )
        part_bbox = draw.textbbox(
            (0, 0),
            part_text,
            font=part_font,
            stroke_width=2,
        )
        part_width = part_bbox[2] - part_bbox[0]
        part_height = part_bbox[3] - part_bbox[1]
        part_y = int(height * 0.515)
        if part_y + part_height > int(height * 0.585):
            part_y = int(height * 0.505)

        _draw_3d_title(
            draw,
            center_x,
            part_y,
            part_text,
            part_font,
            (255, 215, 95),
            align="center",
        )

        print(
            f"🏷️ TITLE PART: {part_text}",
            flush=True,
        )

    # ------------------------------------------------------------
    # Keep the subtitle plaque from the supplied design.
    #
    # It already exists in the template image, so we do NOT redraw it.
    # This avoids the old "box" problem and preserves the exact artwork.
    # ------------------------------------------------------------

    # If the template was not used, create a simple matching plaque.
    if template is None:
        plaque_y = int(height * 0.60)

        draw.rounded_rectangle(
            (
                int(width * 0.18),
                plaque_y,
                int(width * 0.82),
                plaque_y + 62
            ),
            radius=28,
            fill=(12, 19, 25),
            outline=(235, 160, 25),
            width=3
        )

        subtitle_font = find_devanagari_font(30, bold=False)

        subtitle = "एक प्रेरणादायक कहानी"

        bbox = draw.textbbox(
            (0, 0),
            subtitle,
            font=subtitle_font
        )

        draw.text(
            (
                center_x - (bbox[2] - bbox[0]) // 2,
                plaque_y + 12
            ),
            subtitle,
            font=subtitle_font,
            fill=(245, 245, 240)
        )

    title_dir = os.path.join("parts", f"part_{part_no:02d}") if part_no is not None else "images"
    os.makedirs(title_dir, exist_ok=True)
    path = os.path.join(title_dir, "title_card.png")

    img.save(
        path,
        quality=95
    )

    print(
        f"✅ Exact supplied-design title page created: {path}",
        flush=True
    )

    return ImageClip(path).set_duration(duration)
