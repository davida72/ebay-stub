# ebay-stub

Daily sweeper that archives The Smiths ticket-stub listings from eBay.

Uses the **eBay Browse API** (not scraping — eBay 403s all scripted traffic). Every day a GitHub Action searches `"smiths" ticket` across EU + UK marketplaces, then for each listing it hasn't seen before:

1. Downloads every gallery image at the highest resolution eBay serves (`s-l2400`, falling back through `s-l1600` / `s-l1200` / `s-l800`).
2. Writes a Markdown record with the title, price, seller, item specifics, and the seller's description.
3. Tries to extract a gig date from the text (title + specifics + description) and uses it as the folder prefix. Dates must fall in **1982–1986** (The Smiths' active gig years).
4. Drops obvious Morrissey-solo listings.

Results land in `stubs/<gig-date-or-unknown>__<listing-id>/`.

## Setup

### 1. Get eBay API credentials (one-time, free)

1. Sign in at <https://developer.ebay.com/> with your eBay account.
2. Agree to the API License Agreement if prompted.
3. Go to **My Account → Application Keys**.
4. Under **Production**, click **Create a keyset** (give the app any name).
5. Copy the **App ID (Client ID)** and **Cert ID (Client Secret)**.

The Browse API's default rate limit (5,000 calls/day) is far more than this script uses.

### 2. Store the credentials

**For GitHub Actions:**
Repo → **Settings → Secrets and variables → Actions → New repository secret**. Add two secrets:

- `EBAY_CLIENT_ID`
- `EBAY_CLIENT_SECRET`

**For local runs:**

```sh
export EBAY_CLIENT_ID=...
export EBAY_CLIENT_SECRET=...
```

## Run locally

```sh
pip install -r requirements.txt
python smiths_stubs.py                 # live run
python smiths_stubs.py --limit 3 --debug-dates   # 3 listings, log date extraction
```

Re-runs are idempotent — `stubs/index.json` tracks every listing ID the script has already seen (saved or skipped) and never re-fetches it. The OAuth token is cached in `.ebay-token.json` (gitignored) for ~2 hours.

## Scheduled run

`.github/workflows/daily.yml` runs the scraper at 09:00 UTC daily and commits any new `stubs/**` content back to the repo. You can also trigger it manually from the Actions tab (`workflow_dispatch`).

## Known limits

- **No OCR**: dates that only appear on the image itself produce `unknown__<id>` folders — rename them by hand or add Tesseract later.
- **Smart-search quirks**: the website's `_svsrch=1` ("smart match") expands queries in ways the API doesn't fully replicate. If you spot recall gaps vs. the manual browser search, we can add additional query variants.
- **Region**: filtered to EU + UK (`GB, IE, FR, DE, IT, ES, NL, BE, PT, AT, DK, SE, FI, PL, CZ, GR, HU, RO`). Adjust `EU_COUNTRIES` in `smiths_stubs.py` to widen or narrow.
