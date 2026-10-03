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
