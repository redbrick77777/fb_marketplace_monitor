# FB Marketplace Watcher

Watches Facebook Marketplace for listings matching your criteria (keywords,
city + radius, price range, optional condition) and appends new matches to
either a **local .xlsx file** (zero setup) or a **Google Sheet** (needs a
one-time Google Cloud service account, but gives you live access from your
phone/browser). Built on the [SociaVault](https://sociavault.com) API, since
Facebook itself has no official Marketplace API.

Runs as a single-shot script, so **cron owns the schedule** - the app fetches,
filters, dedupes, writes, and exits. Designed to hold multiple independent
searches ("watches") in one config file, each with its own output file/sheet,
so it's reusable across projects, not just one search.

## Before you start: know the tradeoffs

- SociaVault is a third-party scraping service, not an official Meta API.
  There isn't one. Read the earlier conversation notes on the legal grey
  area (`hiQ v. LinkedIn`-style "publicly visible data" argument) before
  relying on this for anything business-critical.
- This app **only finds and logs listings** - it does not message sellers.
  Messaging requires a logged-in Facebook session, which is a different
  (and riskier) problem; see the earlier discussion for why that's
  deliberately out of scope here.
- SociaVault's own docs have some inconsistencies (e.g. conflicting rate
  limit numbers in different places). The client here retries on 429s and
  fails loudly rather than guessing, but double check current limits/pricing
  against your own account before scaling up polling frequency.

## Setup

### 1. Install dependencies

```bash
cd fb_marketplace_monitor
python3 -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Get a SociaVault API key

Sign up at [sociavault.com](https://sociavault.com) (free tier: 50 credits,
no card needed). Copy your API key from the dashboard.

### 3. Configure your watches

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml`: fill in `sociavault_api_key` (or set `SOCIAVAULT_API_KEY`
as an environment variable instead, if you'd rather not put it in the file).

Under `watches`, edit the example entry (or add more) with your keyword
variants, city, radius, and price range. **By default each watch writes to a
local `.xlsx` file** - no Google setup needed at all:

```yaml
output_type: xlsx
output_file: "output/marketplace_finds.xlsx"
sheet_tab: "Tires"   # worksheet name inside that file
```

The file and worksheet are created automatically (with headers) on first
run. Open it anytime in Excel/Numbers/Google Sheets-via-upload - just close
it before a scheduled run fires, or that run will fail cleanly and retry
next time (nothing gets lost - see "How dedup and failures interact" below).

**Prefer a live Google Sheet instead** (e.g. so you can check it from your
phone)? Switch that watch to:

```yaml
output_type: google_sheets
sheet_id: "your_google_sheet_id_here"   # the long ID in the sheet's URL
sheet_tab: "Tires"
```

...and do this one-time setup:

1. In the [Google Cloud Console](https://console.cloud.google.com/), create
   or pick a project, then enable the **Google Sheets API**
   (APIs & Services -> Enable APIs -> search "Google Sheets API").
2. Go to **IAM & Admin -> Service Accounts -> Create Service Account**.
   Any name is fine; no special roles are needed.
3. Open the new service account -> **Keys -> Add Key -> Create new key ->
   JSON**. This downloads a `.json` file - save it somewhere safe, e.g.
   `fb_marketplace_monitor/service_account.json` (don't commit this to git).
4. Open the JSON file and copy the `client_email` value
   (looks like `something@your-project.iam.gserviceaccount.com`).
5. Open your target Google Sheet, click **Share**, and share it with that
   email address with **Editor** access.
6. Add `google_service_account_file: "service_account.json"` to
   `config.yaml` (or set `GOOGLE_SERVICE_ACCOUNT_FILE` as an env var).

You can mix both: some watches on `xlsx`, others on `google_sheets`, in the
same config file.

### 4. Test it

```bash
python run.py --config config.yaml --dry-run --verbose
```

This fetches and filters normally but doesn't write anything (xlsx or
Sheets), so you can check the log output for what *would* be added first.
Run without `--dry-run` once it looks right.

### How dedup and failures interact

A listing is only marked "seen" **after** it's actually been written
successfully. So:
- `--dry-run` never marks anything seen - it's always safe to preview.
- If a write fails (e.g. the xlsx file is open in Excel when cron fires, or
  a Google Sheets API call errors out), that run exits with a non-zero code
  and logs the failure, but the matching listings are *not* marked seen -
  they'll simply be picked up again next scheduled run. Nothing is lost.

### 5. Schedule it with cron

```bash
crontab -e
```

Add a line like (runs every 30 minutes):

```
*/30 * * * * cd /full/path/to/fb_marketplace_monitor && venv/bin/python run.py --config config.yaml >> logs/cron.log 2>&1
```

**Running this on a MacBook:** works fine, but cron skips runs while the lid
is closed (harmless here - dedup means a late run just catches up), and
macOS may block cron from reading/writing folders under Documents/Desktop
via its privacy layer (TCC) - keep the project directly under your home
folder to sidestep that, or grant `/usr/sbin/cron` Full Disk Access in
System Settings.

**Running this on a small rented VM** (a $4-6/mo DigitalOcean/Linode/Vultr
droplet, or Oracle Cloud's free-forever tier) avoids both of those and keeps
it running even when your laptop is off - worth it if you'll use the box for
other scheduled scripts too. Setup is the same `crontab -e` line above, just
without the macOS quirks; if using xlsx output, remember to pull the file
down (`scp`) or sync it somewhere to actually view it.

## CLI reference

```
python run.py --config config.yaml [--watch NAME] [--dry-run] [--verbose]
```

- `--config` - path to the YAML config (default `config.yaml`)
- `--watch` - only run the one watch with this `name` (useful for testing
  a single search without running everything)
- `--dry-run` - fetch/filter but skip writing (xlsx or Sheets)
- `--verbose` - debug-level logging

Exit codes: `0` success, `1` one or more watches failed, `2` config error.

## How matching works

There are two separate things in a watch, and conflating them is what caused
an early "found nothing, even though I can find it by hand" bug:

- **`search_terms`** - the actual text sent to SociaVault's search. Facebook's
  own search relevance is weak on bare number strings - `"285 70 18"` alone
  can return few or zero results even when matching listings clearly exist,
  while `"285 70 18 tires"` (with a plain category word) finds them. Set
  `search_terms` explicitly to whatever you'd actually type into Marketplace's
  search box yourself.
- **`keywords`** - what's required to appear in a listing's title for it to
  count as a real match, checked *after* the search comes back. These are
  normalized (lowercased, punctuation stripped) before comparing, so
  `"285/70R18"`, `"285 70 18"`, and `"LT285/70R18"` all match the same way -
  add as many variants as you expect sellers to type.

If you don't set `search_terms`, it falls back to using `keywords` as the
search query too - fine for word-like keywords, risky for number-only ones
like tire sizes.

`exclude_keywords` works like `keywords`, in reverse, to filter out
irrelevant hits (e.g. wheels-only listings) - checked against the title (and
description too, if `check_description` is on).

**`check_description`** (default `false`): if a listing's title alone
doesn't match any `keywords`, also check its full description before giving
up on it. This costs one extra SociaVault credit *per listing that fails the
title check* - not per listing overall - since description only comes from
the per-listing item-detail endpoint, not the cheaper search endpoint. If a
loose `search_terms` query returns a lot of raw listings and most don't
match by title, this can add up; it's off by default for that reason. Worth
turning on for products where sellers reliably put specs in the description
but not the title - less useful for something like tire sizes, where sellers
almost always put it in the title anyway. If both `check_description` and
`condition_filter` are enabled for the same watch, the item detail is
fetched once per listing and reused for both checks, not fetched twice.

`condition_filter` (e.g. `"used"`) costs one extra SociaVault credit per
*candidate* listing, since condition only comes back from the item-detail
endpoint, not search. Leave it out of a watch to skip that check and save
credits.

**Radius re-check.** `radius_miles` is sent to SociaVault's search too, but
Facebook doesn't strictly honor it - scrolling far enough surfaces listings
well outside the requested radius, the same way it surfaces
keyword-irrelevant ones. The search response's own per-listing `location`
field is city-level only (no coordinates), so precise distance can only be
computed from the item-detail endpoint's `location.latitude`/`longitude` (the
seller's actual pin) - this check runs for every candidate that reaches it,
costing one extra credit unless `check_description`/`condition_filter`
already fetched that same item detail. Listings with no coordinates at all
(ship-only listings with no fixed pickup point) are dropped too, since
there's no way to confirm they're in range. Matching listings get a
`Distance (mi)` column in the output showing how far the actual pin was from
the watch's resolved center.

### Diagnosing "found nothing"

Run with `--verbose` and check the log for two specific lines:
- A `WARNING` saying SociaVault returned **zero raw listings** - the API
  itself found nothing for your search_terms/location, before your keyword
  filters even run. Fix: broaden `search_terms` or widen `radius_miles`.
- A `WARNING` saying raw listings came back but **all failed the keyword
  match** - SociaVault found things, your `keywords` list is what's too
  strict. Fix: add more keyword variants, or check the sample titles logged
  just above it (DEBUG level) to see how sellers are actually wording them.

Either way, a `DEBUG`-level summary line breaks down exactly how many
listings were filtered out at each stage (already-seen, keyword mismatch,
excluded, price, location, condition) so you're never guessing which filter
did it.

## Adding more searches later

Just add another entry under `watches:` in the same config file - each one
gets its own dedupe tracking and can write to a different xlsx file (or
sheet/tab). That's the intended way to reuse this for other projects instead
of the tire search alone.

## Known SociaVault quirks (learned the hard way)

Their documentation contradicts itself in several places, and the live API
doesn't always match either version. Consolidated here so the pattern is
recognizable if something *new* breaks the same way, rather than needing to
be rediscovered from scratch:

- **Search endpoint wants `lat`/`lng`, not `latitude`/`longitude`** - their
  own blog tutorials use the latter; their actual API reference (and the
  live error message, `"lat is required and must be a number"`) confirm the
  former. `sociavault_client.py`'s `search()` uses the correct names.
- **The real search payload is double-wrapped**: `{"success": true, "data":
  {"success": true, "listings": {...}}}` - not a flat `{"listings": [...]}`
  as the docs examples show. `search()` unwraps `data.data` if present.
- **`listings` (and `locations`, from location-search) come back as an
  object keyed by string index** (`"0"`, `"1"`, ...), not a JSON array.
  Both `search()` and `resolve_location()` normalize this to a real list.
- **A failure can hide inside an HTTP 200** - `{"success": false, "error":
  "..."}` with a 200 status, not just via 4xx/5xx. `_get()` checks the
  `success` field explicitly and raises `SociaVaultError` if it's `False`,
  rather than trusting the status code alone.
- **The item-detail endpoint has the same double-wrap as search, but
  `get_item()` didn't handle it** until 2026-08-15 - it returned the raw
  `{"success", "data": {...}, "credits_used"}` envelope unchanged, so every
  caller reading `description`/`location`/`attributes` off it was reading
  the wrong (outer) object and silently got nothing. This was invisible for
  a long time because the callers that existed then failed *open* on
  missing data (no description match -> still keeps the listing if the
  title matched; no condition -> condition_filter treats `None` as "don't
  filter"). It only became visible once the radius re-check was added,
  which fails *closed* on missing location (no coordinates -> drop) -  that
  combination made it look like the radius filter was dropping everything,
  when the real bug was `get_item()` itself. Fixed by unwrapping `data` the
  same way `search()`/`resolve_location()` already do. If a future
  item-detail field looks mysteriously always-empty, check this first.
- **Per-listing location precision differs by endpoint**: `search()`'s
  `location` field is city-level only (`city`/`state`/`display_name`/
  `city_page_id`, no coordinates) - not enough for real distance math. Only
  the item-detail endpoint's `location.latitude`/`longitude` gives the
  seller's actual pin, which is what the radius re-check (see above) relies
  on. Ship-only listings with no fixed pickup point return `location: {}` on
  both endpoints - no coordinates available for those at all.
- **Pagination is non-deterministic, and an empty page does NOT mean the end
  of results.** A mid-sequence page frequently comes back with zero listings
  while *later* pages still have plenty: in one probe "Breitling" returned 6
  listings on page 1, zero on page 2, then 24 on page 3. `search_all_pages()`
  therefore stops only when the **cursor** runs out, never on an empty page -
  breaking early on a zero-length page silently discards most of the results.
  Re-running the identical query minutes later returns a *different* page
  distribution (the zero moves, and the totals differ), so this can't be
  paced around: raising the inter-page delay from 0.5s to 2.0s did not
  eliminate the empty pages, it only shuffled which ones were empty.
- **Search results are not ordered by distance**, so limiting `max_pages` is
  not a way to stay local. Measured against a 30 mi Jacksonville watch, page
  3 was *more* local than page 1 (75% Jacksonville vs 33%); the genuinely
  far-away listings - Los Angeles, Natick MA, McAllen TX - showed up on page
  1, almost certainly because shipping-enabled listings surface nationwide
  regardless of the requested radius. Cutting `max_pages` to 1 would have
  thrown away ~20 nearby listings while keeping the out-of-state ones.

If a future response shape looks "off" (e.g. a field is unexpectedly empty,
or nested differently than expected), the fix pattern that's worked every
time so far: get the **raw** response via `curl` directly (bypassing our
client entirely) and diff it against what the code assumes - don't guess
from behavior alone, since this vendor's actual API and its docs disagree
often enough that assumptions from docs alone haven't been reliable.

## Files

```
fb_marketplace_monitor/
  config.py            # loads/validates config.yaml
  matching.py           # keyword normalization + matching
  geo.py                 # great-circle distance for the radius re-check
  sociavault_client.py  # SociaVault API wrapper (retries, pagination)
  xlsx_writer.py         # local .xlsx output backend (default, no setup)
  sheets_client.py        # Google Sheets output backend (service account auth)
  store.py               # SQLite dedupe store + location cache
  monitor.py             # ties it all together per watch, picks the output backend
  cli.py                 # argparse entrypoint
run.py                  # `python run.py ...` - what cron calls
config.example.yaml     # copy to config.yaml and edit
requirements.txt
```
