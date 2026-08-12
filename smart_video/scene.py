"""Scene construction: TTS + images + subtitles."""

import os
import numpy as np

from moviepy.editor import (
    AudioFileClip,
    CompositeVideoClip,
    concatenate_audioclips,
)
from moviepy.audio.AudioClip import AudioArrayClip

from .config import MIN_DURATION, VIDEO_SIZE
from .voice import clean_tts_text, generate_voice
from .image_generator import generate_image
from .branding import create_fullscreen_clip
from .subtitles import create_subtitle


# ============================================================
# CINEMATIC IMAGE MOTION
# ============================================================

def _apply_ken_burns(
    clip,
    clip_duration,
    scene_start_time,
    scene_duration,
):
    """
    Apply a continuous cinematic camera move.

    V5 design goal:
      - The camera NEVER reverses direction at an image boundary.
      - Zoom and horizontal drift are based on the GLOBAL scene time,
        not the individual image time.
      - This prevents the visible "forward -> backward" reset that
        makes a slideshow feel mechanical.
      - Motion is deliberately subtle: the viewer should feel that
        the camera is slowly moving through one continuous shot.
    """

    if clip_duration <= 0 or scene_duration <= 0:
        return clip

    from PIL import Image

    # Very restrained camera movement.
    # The scale is continuous across the whole scene.
    ZOOM_START = 1.035
    ZOOM_END = 1.085

    # Only a small fraction of the available crop is traversed.
    # This is intentionally much smaller than a typical Ken Burns effect.
    PAN_START = 0.16
    PAN_END = 0.84

    def smoothstep(t):
        t = max(0.0, min(1.0, t))
        return t * t * (3.0 - 2.0 * t)

    def transform(get_frame, local_t):
        frame = get_frame(local_t)

        h, w = frame.shape[:2]

        # IMPORTANT:
        # Convert local clip time to GLOBAL scene time.
        #
        # Example with 3 images:
        # image 1 -> progress 0.00 -> ~0.33
        # image 2 -> progress ~0.33 -> ~0.66
        # image 3 -> progress ~0.66 -> 1.00
        #
        # Therefore the camera never jumps back to the beginning
        # when a new image appears.
        global_time = scene_start_time + local_t

        progress = (
            global_time / scene_duration
            if scene_duration > 0
            else 1.0
        )

        progress = max(
            0.0,
            min(1.0, progress)
        )

        eased = smoothstep(progress)

        zoom = (
            ZOOM_START
            + (
                ZOOM_END - ZOOM_START
            ) * eased
        )

        new_w = max(
            w,
            int(w * zoom)
        )

        new_h = max(
            h,
            int(h * zoom)
        )

        pil = Image.fromarray(
            frame.astype("uint8")
        )

        pil = pil.resize(
            (new_w, new_h),
            Image.Resampling.LANCZOS
        )

        arr = np.asarray(pil)

        max_x = max(
            0,
            new_w - w
        )

        max_y = max(
            0,
            new_h - h
        )

        # Continuous LEFT -> RIGHT camera travel.
        #
        # Every image uses the SAME global camera position.
        # This is the key fix for the backward jump.
        pan_progress = (
            PAN_START
            + (
                PAN_END - PAN_START
            ) * eased
        )

        x = int(
            max_x * pan_progress
        )

        # Keep vertical framing stable.
        y = int(
            max_y * 0.50
        )

        x = max(
            0,
            min(x, max_x)
        )

        y = max(
            0,
            min(y, max_y)
        )

        cropped = arr[
            y:y + h,
            x:x + w
        ]

        if (
            cropped.shape[0] != h
            or cropped.shape[1] != w
        ):
            cropped = np.asarray(
                Image.fromarray(
                    cropped.astype("uint8")
                ).resize(
                    (w, h),
                    Image.Resampling.LANCZOS
                )
            )

        return cropped.astype("uint8")

    return clip.fl(
        lambda gf, t: transform(gf, t)
    )


def create_scene(
    scene,
    index,
    part_no=1
):

    part_no = int(part_no)
    scene_number = scene.get(
        "scene_number",
        index + 1
    )

    part_dir = os.path.join(
        "parts",
        f"part_{part_no:02d}"
    )
    image_dir = os.path.join(part_dir, "images")
    audio_dir = os.path.join(part_dir, "audio")
    os.makedirs(image_dir, exist_ok=True)
    os.makedirs(audio_dir, exist_ok=True)

    print(
        f"\n🎬 Scene {scene_number}",
        flush=True
    )

    # --------------------------------------------------------
    # Main scene text comes directly from MongoDB.
    #
    # IMPORTANT:
    # The main text is used for ONE voice track.
    # sub_image_prompts only controls which images are shown.
    # --------------------------------------------------------

    text = clean_tts_text(
        scene["text"]
    )

    if not text:
        raise ValueError(
            f"❌ Scene {scene_number} contains empty narration text"
        )

    # --------------------------------------------------------
    # MongoDB structure:
    #
    # "sub_image_prompts": [
    #   {
    #       "text": "...",
    #       "image_prompt": "..."
    #   },
    #   ...
    # ]
    # --------------------------------------------------------

    sub_image_prompts = scene[
        "sub_image_prompts"
    ]

    if not sub_image_prompts:
        raise ValueError(
            f"❌ Scene {scene_number} contains no image prompts"
        )

    print(
        f"📝 Text: {text[:150]}...",
        flush=True
    )

    print(
        f"🎨 Sub-image prompts: "
        f"{len(sub_image_prompts)}",
        flush=True
    )

    for prompt_index, item in enumerate(
        sub_image_prompts,
        start=1
    ):

        sub_text = item.get(
            "text",
            ""
        )

        image_prompt = item[
            "image_prompt"
        ]

        print(
            f"   🖼️ Image "
            f"{prompt_index}/{len(sub_image_prompts)}",
            flush=True
        )

        if sub_text:

            print(
                f"      📝 Sub-text: "
                f"{sub_text[:100]}...",
                flush=True
            )

        print(
            f"      🎨 Prompt: "
            f"{image_prompt[:120]}...",
            flush=True
        )

    # --------------------------------------------------------
    # Generate ONE voice for the complete scene text
    # --------------------------------------------------------

    audio_path = (
        os.path.join(
            audio_dir,
            f"a_{scene_number:03d}.mp3"
        )
    )

    voice, word_timings = generate_voice(
        text,
        audio_path
    )

    audio = None

    if (
        voice
        and os.path.exists(audio_path)
    ):

        try:

            audio = AudioFileClip(
                audio_path
            )

        except Exception as e:

            print(
                f"⚠️ Generated audio could "
                f"not be opened: {e}",
                flush=True
            )

            audio = None

    duration = max(
        audio.duration
        if audio
        else 0,
        MIN_DURATION
    )

    print(
        f"⏱️ Scene voice/video duration: "
        f"{duration:.2f}s",
        flush=True
    )

    if word_timings:

        first_word = word_timings[0]
        last_word = word_timings[-1]

        print(
            f"⏱️ First word: "
            f"{first_word['word']} "
            f"@ {first_word['start']:.3f}s",
            flush=True
        )

        print(
            f"⏱️ Last word: "
            f"{last_word['word']} "
            f"@ {last_word['start']:.3f}s",
            flush=True
        )

    # --------------------------------------------------------
    # Generate all images
    # --------------------------------------------------------

    image_clips = []

    image_count = len(
        sub_image_prompts
    )

    print(
        f"🖼️ Total images for scene: "
        f"{image_count}",
        flush=True
    )

    if image_count == 1:
        print(
            "ℹ️ Only 1 image prompt supplied for this scene.",
            flush=True
        )
    elif image_count == 2:
        print(
            "🎞️ 2-image cinematic sequence enabled.",
            flush=True
        )
    else:
        print(
            f"🎞️ {image_count}-image cinematic sequence enabled.",
            flush=True
        )

    # --------------------------------------------------------
    # Cinematic image transitions
    # --------------------------------------------------------
    #
    # IMPORTANT:
    # The old implementation started every image exactly when
    # the previous image ended. A crossfade on a non-overlapping
    # clip can expose transparent/black frames.
    #
    # We now create a REAL overlap:
    #
    # Image 1  --------------------
    #                    \\
    #                     \\ Image 2
    #                      ----------------
    #
    # The next image starts CROSSFADE_DURATION before the current
    # image ends. Therefore there is always an opaque image behind
    # the incoming image.
    # --------------------------------------------------------

    # --------------------------------------------------------
    # MOVIE-STYLE IMAGE TRANSITIONS
    # --------------------------------------------------------
    #
    # Each image owns one "story beat" of the narration.
    #
    # For N images:
    #
    #   slot = narration_duration / N
    #
    # The next image starts slightly before the previous image
    # finishes. This creates a true dissolve instead of:
    #
    #   image -> transparent -> black -> image
    #
    # IMPORTANT:
    # We do NOT concatenate these clips. We composite overlapping
    # clips so there is always a visible outgoing image underneath
    # the incoming image.
    # --------------------------------------------------------

    CROSSFADE_DURATION = 0.55

    base_image_duration = (
        duration / image_count
    )

    # Keep the transition proportional for very short scenes.
    transition_duration = min(
        CROSSFADE_DURATION,
        max(
            0.12,
            base_image_duration * 0.22
        )
    )

    print(
        f"🎞️ Image slot duration: "
        f"{base_image_duration:.2f}s",
        flush=True
    )

    print(
        f"🎞️ Cinematic crossfade: "
        f"{transition_duration:.2f}s",
        flush=True
    )

    for prompt_index, item in enumerate(
        sub_image_prompts,
        start=1
    ):

        image_prompt = item[
            "image_prompt"
        ]

        if not image_prompt:
            raise ValueError(
                f"❌ Scene {scene_number}, image "
                f"{prompt_index} has an empty image_prompt"
            )

        img_path = (
            os.path.join(
                image_dir,
                f"s_{scene_number:03d}_{prompt_index:02d}.png"
            )
        )

        print(
            f"🖼️ Generating image "
            f"{prompt_index}/{image_count}",
            flush=True
        )

        img = generate_image(
            image_prompt,
            img_path,
            text
        )

        if not img:
            raise RuntimeError(
                f"❌ Image generator returned no image "
                f"for scene {scene_number}, "
                f"image {prompt_index}"
            )

        # ----------------------------------------------------
        # REAL OVERLAP TIMING
        # ----------------------------------------------------
        #
        # Image 1:
        #   0.00 -> slot + transition
        #
        # Image 2:
        #   slot - transition -> 2*slot
        #
        # Image 3:
        #   2*slot - transition -> narration end
        #
        # Therefore the outgoing image is still visible while the
        # next image fades in.
        # ----------------------------------------------------

        start_time = (
            0.0
            if prompt_index == 1
            else (
                (prompt_index - 1)
                * base_image_duration
                - transition_duration
            )
        )

        if prompt_index < image_count:
            clip_duration = (
                base_image_duration
                + transition_duration
            )
        else:
            clip_duration = (
                duration
                - start_time
            )

        clip_duration = max(
            0.10,
            clip_duration
        )

        end_time = (
            start_time
            + clip_duration
        )

        print(
            f"   🎞️ Image {prompt_index}: "
            f"{start_time:.2f}s -> "
            f"{end_time:.2f}s "
            f"({clip_duration:.2f}s)",
            flush=True
        )

        image_clip = create_fullscreen_clip(
            img,
            clip_duration,
            prompt_index
        )

        if image_clip is None:
            raise RuntimeError(
                f"❌ Could not create video clip "
                f"for scene {scene_number}, "
                f"image {prompt_index}"
            )

        # ----------------------------------------------------
        # KEN BURNS CAMERA
        # ----------------------------------------------------

        image_clip = _apply_ken_burns(
            image_clip,
            clip_duration,
            start_time,
            duration,
        )

        # ----------------------------------------------------
        # TRUE DISSOLVE
        # ----------------------------------------------------

        if prompt_index > 1:

            image_clip = image_clip.crossfadein(
                transition_duration
            )

        image_clips.append(
            (
                start_time,
                image_clip
            )
        )

    # --------------------------------------------------------
    # COMPOSITE ALL IMAGE CLIPS
    # --------------------------------------------------------
    #
    # No concatenate_videoclips() here.
    #
    # CompositeVideoClip is essential because the clips overlap.
    # --------------------------------------------------------

    base = CompositeVideoClip(
        [
            clip.set_start(
                start_time
            )
            for start_time, clip
            in image_clips
        ],
        size=VIDEO_SIZE
    ).set_duration(
        duration
    )

    # --------------------------------------------------------
    # Create subtitle for the complete scene text
    # --------------------------------------------------------

    subtitle_duration = (
        audio.duration
        if audio
        else duration
    )

    subtitle = create_subtitle(
        text,
        subtitle_duration
    )

    # --------------------------------------------------------
    # Combine animated image + subtitles.
    # --------------------------------------------------------

    final = CompositeVideoClip(
        [
            base,
            subtitle
        ],
        size=VIDEO_SIZE
    ).set_duration(
        duration
    )

    # --------------------------------------------------------
    # Add scene voice
    # --------------------------------------------------------

    if audio:

        if audio.duration < duration:

            silence = AudioArrayClip(
                np.zeros(
                    (
                        int(
                            44100
                            * (
                                duration
                                - audio.duration
                            )
                        ),
                        2
                    )
                ),
                fps=44100
            )

            audio = concatenate_audioclips(
                [
                    audio,
                    silence
                ]
            )

        else:

            audio = audio.subclip(
                0,
                duration
            )

        final = final.set_audio(
            audio
        )

    print(
        f"✅ Scene {scene_number} created "
        f"with continuous cinematic camera motion + "
        f"{transition_duration:.2f}s movie-style dissolve",
        flush=True
    )

    return final
