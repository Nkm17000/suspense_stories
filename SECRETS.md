# GitHub repository secrets

Required:
- `MONGODB_URI`
- `CLOUDFLARE_ACCOUNT_ID_1`
- `CLOUDFLARE_API_TOKEN_1`
- `CLOUDFLARE_ACCOUNT_ID_2`
- `CLOUDFLARE_API_TOKEN_2`
- `CLOUDFLARE_ACCOUNT_ID_3`
- `CLOUDFLARE_API_TOKEN_3`
- `FB_PAGE_ID`
- `FB_PAGE_ACCESS_TOKEN`
- `INSTAGRAM_BUSINESS_ACCOUNT_ID`
- `INSTAGRAM_ACCESS_TOKEN`

Optional:
- `STORY_ID`
- `MONGODB_DATABASE` (default `storydb`)
- `MONGODB_COLLECTION` (default `longstory`)
- Cloudflare model/steps/retry/timeout settings
- Pollinations retry/timeout settings

Important:
- Never commit `.env`, API tokens, access tokens, or MongoDB credentials.
- The Cloudflare API token needs permission to run Workers AI inference for the selected account.
- Instagram publishing requires an eligible Professional Instagram account and a valid Meta access token with the permissions required by the Reels Content Publishing API.
- The neuron estimate is used for routing; MongoDB is the persistent account/day ledger. Cloudflare's dashboard remains authoritative for actual billed usage.

## Cloudflare multi-account rotation

Use numbered GitHub repository secrets. These are API credentials, not passwords:

- `CLOUDFLARE_ACCOUNT_ID_1`
- `CLOUDFLARE_API_TOKEN_1`
- `CLOUDFLARE_ACCOUNT_ID_2`
- `CLOUDFLARE_API_TOKEN_2`
- `CLOUDFLARE_ACCOUNT_ID_3`
- `CLOUDFLARE_API_TOKEN_3`
- `CLOUDFLARE_ACCOUNT_ID_4`
- `CLOUDFLARE_API_TOKEN_4`
- `CLOUDFLARE_ACCOUNT_ID_5`
- `CLOUDFLARE_API_TOKEN_5`

The workflow can use `CLOUDFLARE_ACCOUNT_COUNT=3` (or more). It switches to the next account when the next image would push the current account above the 8,500-neuron UTC-day threshold. Do not store Cloudflare credentials in the JSON story document.

MongoDB collection for the daily account ledger:

`cloudflare_daily_usage`

One document is maintained per UTC date + Cloudflare account ID, including `neurons_used`, `estimated_neurons`, `reported_neurons`, and `image_count`.
