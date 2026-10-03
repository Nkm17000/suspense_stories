# Suspense Stories

Suspense-story video pipeline for Smart Learning Lab.

## Pipeline

1. Claim the next `PENDING` story from MongoDB.
2. Validate each part and its 10 scenes.
3. Generate each scene image with **Cloudflare Workers AI first**.
4. If Cloudflare fails or the local neuron ledger reaches the configured threshold, use **Pollinations as fallback**.
5. Print and log the exact final image prompt sent to the provider.
6. Print per-image and per-story Cloudflare neuron usage. The usage is estimated unless the API returns an explicit usage value.
7. Build each part video.
8. Publish the completed part to Facebook and Instagram Reel.
9. Save provider/media IDs and statuses back to MongoDB.
10. Continue to the next part sequentially.

## Important prompt convention

- Story/narration/dialogue: Hindi only.
- `scene_prompt`: English only, with no Devanagari/Hindi characters.
- Every image prompt is self-contained and repeats the full visual character details because image requests are sent one at a time.

See:
- `DATABASE_SCHEMA.md`
- `SECRETS.md`
- `examples/suspense_story_sample.json`
- `prompt.py`


## Execution and Cloudflare account rotation

- Scheduled GitHub Actions execution runs once per day.
- `workflow_dispatch` always starts the workflow immediately when manually triggered.
- A story with `status=PROCESSING` is resumed when it still contains parts with `status=PENDING`.
- Only `PENDING` parts are rendered and published. `SUCCESS`, `FAILED`, and `SKIPPED` parts are not rebuilt unless their part status is changed back to `PENDING`.
- Cloudflare credentials use numbered pairs: `CLOUDFLARE_ACCOUNT_ID_1` / `CLOUDFLARE_API_TOKEN_1`, through `_5`.
- The MongoDB collection `cloudflare_daily_usage` tracks neurons by UTC date and account ID.
- An account is selected only when its reserved daily neuron total can remain at or below 8,300. The next account is selected when the threshold is reached.
- Each `part_results.part_XX` stores `cloudflare_neurons_used`, `estimated_cloudflare_neurons`, and `reported_cloudflare_neurons`.


## Cloudflare multi-account neuron routing

The image generator uses Cloudflare Workers AI first and Pollinations only as a
fallback. Configure `CLOUDFLARE_ACCOUNT_COUNT` plus numbered account/token
pairs (`CLOUDFLARE_ACCOUNT_ID_1`, `CLOUDFLARE_API_TOKEN_1`, etc.).

For every image request, the router atomically reserves the estimated FLUX
neurons in MongoDB under:

- `cloudflare_daily_usage`
- `date_utc` (UTC calendar day)
- `account_id`

The default routing threshold is **8,500 neurons per account per UTC day**.
The configured accounts are tried in order. When the next image would push an
account above 8,500, that account is skipped and the next account is selected.
A failed Cloudflare request releases its reservation before another account is
tried.

After a successful image, the reservation is settled exactly once. If an
explicit neuron count is present in the API response, the ledger is corrected
to that count; otherwise the configured estimate is retained.

Pollinations is called only when all eligible Cloudflare accounts fail or no
account can reserve capacity for the image. It is not used after a successful
Cloudflare image.

The Cloudflare Workers AI REST endpoint used is:

`POST https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/run/@cf/black-forest-labs/flux-1-schnell`

with the account-specific bearer token and JSON payload containing `prompt`
and `steps`.
