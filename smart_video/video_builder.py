"""Build one MP4 for one long-story part."""

import os

from moviepy.editor import concatenate_videoclips, vfx

from .config import FPS, CTA_URL, END_CARD_DURATION, TITLE_CARD_DURATION
from .title_card import create_title_card
from .scene import create_scene
from .end_card import create_end_card


def build_part_video(scenes, title, part_no, part_title=None):
    """Build exactly one video from the scenes belonging to one part."""
    clips = []
    transition_duration = 0.40
    part_no = int(part_no)
    part_dir = os.path.join("parts", f"part_{part_no:02d}")
    os.makedirs(part_dir, exist_ok=True)

    print("==========================================", flush=True)
    print(f"🎬 BUILDING PART {part_no}", flush=True)
    print(f"📖 Story title : {title}", flush=True)
    print(f"🧩 Part title  : {part_title or f'Part {part_no}'}", flush=True)
    print(f"🎞️ Scenes      : {len(scenes)}", flush=True)
    print("==========================================", flush=True)

    try:
        title_clip = create_title_card(
            title,
            TITLE_CARD_DURATION,
            part_no=part_no,
        )
        clips.append(title_clip)

        for i, scene in enumerate(scenes):
            print(
                f"\n🎬 Part {part_no} - scene {i + 1}/{len(scenes)}",
                flush=True,
            )
            clip = create_scene(
                scene,
                i,
                part_no=part_no,
            )
            if clip is None:
                raise ValueError(
                    f"Part {part_no}, scene {i + 1} returned no video clip"
                )
            if clip.duration is None or clip.duration <= 0:
                raise ValueError(
                    f"Part {part_no}, scene {i + 1} has invalid duration: {clip.duration}"
                )
            clips.append(clip)

        if len(clips) <= 1:
            raise ValueError(f"Part {part_no} has no valid story scenes")

        print(
            f"\n📣 Adding final CTA to Part {part_no}: {CTA_URL}",
            flush=True,
        )
        clips.append(create_end_card(END_CARD_DURATION))

        transitioned = []
        for index, clip in enumerate(clips):
            if index == 0:
                transitioned.append(clip)
                continue

            fade = min(
                transition_duration,
                max(0.05, clip.duration * 0.30),
            )
            print(
                f"🎞️ Part {part_no} crossfade before clip {index + 1}: {fade:.2f}s",
                flush=True,
            )
            transitioned.append(clip.crossfadein(fade))

        final = concatenate_videoclips(
            transitioned,
            method="compose",
            padding=-transition_duration,
        )

        final = final.fx(vfx.speedx, 1.15)

        output_path = os.path.abspath(
            os.path.join(part_dir, f"part_{part_no:02d}.mp4")
        )

        print(
            f"\n🎬 Part {part_no} final duration (1.15x): {final.duration:.2f}s",
            flush=True,
        )
        print(f"💾 Writing: {output_path}", flush=True)

        final.write_videofile(
            output_path,
            fps=FPS,
            codec="libx264",
            audio_codec="aac",
            threads=2,
        )

        if not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
            raise RuntimeError(
                f"Part {part_no} video was not created or is empty"
            )

        print(f"✅ Part {part_no} video created: {output_path}", flush=True)
        return output_path

    finally:
        for clip in clips:
            try:
                clip.close()
            except Exception:
                pass
