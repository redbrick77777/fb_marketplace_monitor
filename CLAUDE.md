# Working on this project

## File edits: use the Read/Edit/Write tools, not shell text-munging

Make file changes with the `Edit` / `Write` tools (after `Read`ing the file) -
**not** with shell one-liners (`sed -i`, `>> file`, `printf > file`) and **not**
with Python heredocs of the form:

```python
python3 - <<'PY'
p = "some/file.py"
s = open(p).read()
s = s.replace("old", "new")   # <-- silently does nothing if "old" isn't exact
open(p, "w").write(s)
PY
```

Why it matters:

- `str.replace()` and `sed` **no-op silently** when the pattern doesn't match
  exactly. A typo'd search string looks like a successful edit, and the damage
  surfaces later somewhere apparently unrelated. `Edit` fails loudly instead:
  it errors if `old_string` isn't found, and errors again if it matches more
  than one place.
- The tools render a reviewable diff in the app. Shell redirection doesn't, so
  changes land unseen.

Only drop to a script when `Edit` genuinely cannot express the change - for
example a programmatic bulk transform across many files, where the edit is
defined by a rule rather than by specific known strings.

This rule is about **writes**. Reading and analysis are unaffected: `grep`,
`sqlite3` queries, `git log`, and throwaway scripts that only compute and print
are all fine.

## Throwaway scripts go in `tmp/`, inside the repo

Write one-off probes, diagnostics and analysis scripts to `tmp/` in the project
directory - not to `/tmp` or anywhere else outside it. `tmp/` is gitignored, so
nothing lands in a commit, but the scripts stay visible alongside the code and
can be re-run or adapted later instead of being lost.

Permanent tests belong in `tests/` and are committed.

## Never run `run.py` without asking first

Bash is generally permitted in this directory, so run tests, probes, queries and
one-off analysis scripts freely without checking in. **The monitor itself is the
exception.**

`run.py` (and anything else invoking `run_all`) is the production script. A run
spends real SociaVault credits, appends rows to the user's live Google Sheets,
and permanently marks listings as seen in `data/seen_listings.db` - and "seen" is
a one-way door: the code decides each listing exactly once and never re-checks
it, so a bad run means legitimate listings are silently skipped until someone
manually deletes rows from the database. **Always ask before running it**, even
when the change looks obviously safe.

Cron already runs it every 3 hours (`0 */3 * * *`), so a manual run is rarely
urgent - waiting for the next scheduled fire is usually a fine substitute. To
test logic without side effects, use `tests/test_prefilter.py`, which stubs the
API and hits neither the network nor real credits.

## Confirm changes to output format or settings before making them

The output columns and the config file are the user's surfaces, not
implementation details. Get an explicit yes before changing:

- `HEADER_ROW` or the row layout in `sheets_client.py` / `xlsx_writer.py`
- anything in `config.yaml` (watches, keywords, radii, `max_pages`, output
  targets)

Why: the `Distance (mi)` column was added mid-header, after `Price`, while the
live Google Sheets tabs still carried the old 6-column header. Every column from
`Link` onward shifted one position, and `sheets_client` logs a warning about the
mismatch and then appends anyway - it neither migrates nor refuses. Rows written
from 2026-08-13 onward were misaligned against their labels, and the user had to
repair both spreadsheets by hand.

If a column genuinely must be added, **append it last** so already-populated
sheets stay aligned, and say up front what existing tabs will need.

## Timestamps: the database is UTC, everything human-facing is EDT

`seen_listings.first_seen` is stored in **UTC** (`...+00:00`). The droplet's
local zone is **EDT (UTC-4)**, and every timestamp a human reads here - `git
log`, the cron schedule, `logs/monitor.log` lines - is local.

Always convert to UTC before comparing against `first_seen`. Getting this wrong
is not theoretical: a cleanup query written with a local-time cutoff was 4 hours
too early, and would have re-processed listings that had already been written to
the sheets, duplicating them.
