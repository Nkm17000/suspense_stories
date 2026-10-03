# Suspense Stories MongoDB schema

Database: `storydb`
Collection: `longstory`

Recommended document shape:

- `story_id`: string, unique application ID
- `story_no`: integer, optional ordering field
- `title`: Hindi string
- `status`: `PENDING | PROCESSING | SUCCESS | PARTIAL_SUCCESS | FAILED`
- `category`: Hindi category
- `language`: `hi`
- `style`: image/video style
- `characters.MAIN`: `{id, name, description}`
- `characters.SUPPORT`: `{id, name, description}`
- `parts[]`: each part contains:
  - `part_no`: integer
  - `part_title`: Hindi
  - `scenes[]`: exactly 10 scenes
- `scenes[]`:
  - `scene_number`: integer
  - `text`: Hindi narration/dialogue
  - `sub_image_prompts[]`: 1–2 image requests
    - `text`: Hindi short visual beat
    - `scene_prompt`: English-only final image prompt
- Runtime fields added by the pipeline:
  - `processing_at`
  - `updated_at`
  - `completed_at`
  - `last_error`
  - `part_results.part_XX`
  - `image_usage`
  - `usage_report_path`

Create a unique index on `story_id` if it is not already unique.
