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

## Cloudflare daily usage collection

Collection: `cloudflare_daily_usage`

Example document:

```json
{
  "date_utc": "2026-10-02",
  "account_id": "cloudflare-account-id",
  "neurons_used": 7425.6,
  "reserved_neurons": 7425.6,
  "estimated_neurons": 7425.6,
  "reported_neurons": 0,
  "image_count": 1547,
  "created_at": "2026-10-02T00:00:00Z",
  "updated_at": "2026-10-02T12:00:00Z"
}
```

The application atomically reserves estimated neurons before a Cloudflare request. An account is not selected when the reservation would make `neurons_used` greater than `8300` for that UTC day.
