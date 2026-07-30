# repo-watcher

A GitHub Actions workflow that watches job-board repos and sends Telegram alerts when new listings appear.

Currently watches **6 sources across 4 repos** (see the `WATCHERS` list in `.github/workflows/watch-files.yml` for the source of truth — Speedyapply is implemented as 3 entries, one per category table, sharing one label):

| Watcher label | Repo | Branch | File | Scope |
|---|---|---|---|---|
| Simplify Off-Season Repo | `SimplifyJobs/Summer2026-Internships` | `dev` | `README-Off-Season.md` | Software Engineering section only |
| Simplify Summer Repo | `SimplifyJobs/Summer2026-Internships` | `dev` | `README.md` | Software Engineering section only |
| Vansh Off-Season Repo | `vanshb03/Summer2027-Internships` | `dev` | `OFFSEASON_README.md` | full listing table |
| Vansh Summer Repo | `vanshb03/Summer2027-Internships` | `dev` | `README.md` | full listing table |
| Zapply Summer Repo | `zapplyjobs/Internships-2027` | `main` | `README.md` | Software Engineering section only |
| Speedyapply Summer Repo | `speedyapply/2027-SWE-College-Jobs` | `main` | `README.md` | USA SWE Internships — all 3 tables: FAANG+, Quant, Other |

The two **Simplify** watchers intentionally parse only the `## 💻 Software Engineering Internship Roles` section — Product Management, Data Science/AI/ML, Quant Finance, and Hardware roles are excluded by design. The two **Vansh** watchers parse the entire `## The List` table, which is uncategorized (so non-SWE roles do flow through from those sources). The **Zapply** watcher parses only the `💻 Software Engineering` table of `zapplyjobs/Internships-2027`; its other five category tables (Data Science & AI, Hardware & Engineering, Product/Design/Research, Business & Operations, Other) are excluded by design. The **Speedyapply** source (`speedyapply/2027-SWE-College-Jobs` → `README.md`) watches the **USA SWE Internships** page in full — all three of its category tables (FAANG+, Quant, Other) — via three watcher entries that share the single "Speedyapply Summer Repo" label and stamp the category into the alert's last field; the repo's separate New-Grad and International pages (`NEW_GRAD_USA.md`, `INTERN_INTL.md`, `NEW_GRAD_INTL.md`) are not watched.

## Delivery guarantee

Every source uses one dedup rule, built around a per-opening **identity**:

```
identity = (apply_url or ROWKEY:company|role|location) + "|" + term + "#" + occurrence
```

A listing is alerted when its identity has never been delivered. Three properties make that safe:

1. **`seen` only grows.** It is a union, never a replacement. When the parsed row set shrinks — a truncated parse, or upstream pruning its table — the rows that vanished stay in `seen`, so they do not re-alert when they come back, while anything genuinely new in the same run still goes out.
2. **An identity is recorded only after Telegram confirms the message.** A failed or rate-limited send leaves it unseen, and `last_sha` is held back so the next run re-fetches and retries. Nothing is ever marked seen without having been delivered.
3. **Every parsed row gets exactly one identity, and no two rows in a run share one.** The apply URL separates openings that are textually identical (Copart posts several Dallas SWE-intern reqs differing only by Workday ID); the `ROWKEY` fallback covers rows with no parseable link (~a fifth of the Vansh rows); `term` lets a requisition relisted for a new season through; the occurrence index separates rows identical even down to a missing URL.

Failure modes that remain all produce a *duplicate*, never a miss: a URL gaining or losing tracking parameters, URL-less rows being reordered, or `SEEN_CAP` eviction (logged when it happens).

State lives in `.watcher_state.json`, one entry per source:

```json
{ "<state_key>": { "last_sha": "...", "seen": ["<identity>", ...],
                   "seen_legacy_urls": ["<url>", ...], "last_row_count": 28 } }
```

`seen_legacy_urls` holds bare apply URLs recorded before identities existed, by the two sources that used cumulative-URL dedup. Those predate the term suffix, so they can only be matched on URL. The set is static and can be dropped after a season. Migration to this shape runs inline, is idempotent, and seeds from both the stored rows and every job in `.bot_state.json`, so the first run after a deploy alerts nothing.

Identities are stored in full rather than hashed, so `git log -p .watcher_state.json` shows exactly which listings each run added. The cost is size: the file is committed on most runs, and it grows by roughly a thousand identities a year on the busiest source. `SEEN_CAP` (5000 per source) is a backstop against a runaway parse, not the expected size.

## Triggering (external cron — NOT a GitHub schedule)

> **This workflow is triggered externally by [cron-job.org](https://cron-job.org), by
> design. It deliberately does *not* use a GitHub Actions `schedule:` cron.** The `on:`
> block in `.github/workflows/watch-files.yml` is intentionally limited to
> `workflow_dispatch:` — do **not** add a `schedule:` trigger.

A cron-job.org job runs every 1 minute and fires the workflow via the GitHub REST API's
`workflow_dispatch` endpoint:

- **Method:** `POST`
- **URL:** `https://api.github.com/repos/kiankian/repo-watcher/actions/workflows/watch-files.yml/dispatches`
- **Body:** `{"ref":"main"}`
- **Headers:**
  - `Accept: application/vnd.github+json`
  - `X-GitHub-Api-Version: 2022-11-28`
  - `Authorization: Bearer <GITHUB_PAT>`

A successful dispatch returns **HTTP 204** (no body). The `<GITHUB_PAT>` must be a token
authorized to dispatch workflows on this repo:

- **Fine-grained PAT:** Repository access = this repo, with **Actions: Read and write**.
- **Classic PAT:** `workflow` scope (or `repo`).

> ⚠️ **PATs expire.** When the token behind cron-job.org expires or is revoked, the
> dispatch starts returning **401/403**, the workflow stops running, and Telegram alerts go
> silent — with no error visible in this repo. This is the most common cause of an outage.
> See the runbook below.

## Logs and health

Two append-only logs are committed alongside the state, rotated monthly:

| File | One line per | Use |
|---|---|---|
| `logs/alerts-YYYY-MM.jsonl` | **delivered** Telegram message | `{ts, seq, message_id, watcher, identity, job_hash, company, role, location, term, apply_url}` |
| `logs/runs-YYYY-MM.jsonl` | watcher per run that did something, plus an hourly heartbeat | `{ts, run_id, watcher, state_key, prev_sha, latest_sha, rows_extracted, prev_row_count, seen_size, identities_new, sent_ok, sent_failed, skip_reason}` |

The alert log exists because `.bot_state.json` cannot serve as one: `pending` is a dict keyed by job hash, so a re-sent alert **overwrites its own record** and the evidence disappears. Reconstructing past duplicates meant diffing gaps in Telegram message IDs; these logs make it a one-line query. The run log turns a shrinking parse into a visible time series rather than something only findable by diffing state commits.

Quiet runs are collapsed into the hourly heartbeat on purpose — at one dispatch a minute, logging unconditionally would be ~8,600 lines a day.

### Sequence numbers

Each alert carries a number (`Simplify Summer Repo #1032`). It is committed only after Telegram confirms the send, so the sequence has no gaps: **a gap in the numbering in your chat is proof of a lost alert**, verifiable without touching this repo. `process_applies` preserves the number when it edits a message to `✅ Logged`.

### Health alerts

Operational faults are sent to the same chat with a `⚠️ watcher:` prefix, rate-limited to one per kind per 30 minutes:

- a source parsed **0 rows** (renamed heading or reshaped table — otherwise indistinguishable from "no new listings")
- a parse returned under 70% of its previous row count
- a send failed (the listing stays unrecorded and will be retried)
- `SEEN_CAP` eviction
- no successful run for over 2 hours

> ⚠️ **The 2-hour silence check can only fire during a run that actually happens.** It catches the workflow erroring, or the dispatch stalling and recovering — it *cannot* detect the dispatch stopping for good, which is the most likely outage (see below). Closing that gap needs an external dead-man switch: set the `HEALTHCHECK_PING_URL` secret to a [healthchecks.io](https://healthchecks.io)-style ping URL and the workflow will hit it at the end of every successful run, leaving that service to notify you when the pings stop. Unset, the ping is skipped.

### Dry runs

Run the workflow from the Actions tab with **dry_run** checked to parse live upstream data, compute identities, and print what *would* be sent — without sending a message, writing state, or committing. Use it to rehearse a change before any alert can go out.

### If alerts stop (troubleshooting runbook)

Because nothing in *this* repo triggers the watcher, a silent stop is almost always on the
cron-job.org / token side. Diagnose in this order:

1. **cron-job.org → the job's execution history.**
   - Job **disabled / no recent executions** → re-enable it. cron-job.org auto-disables a
     job after repeated consecutive failures.
   - Executions showing **401 / 403** → the PAT is expired, revoked, or lacks
     `Actions: write`. Mint a new PAT (see above) and update the cron-job.org request's
     `Authorization` header.
   - Executions showing **204** but still no alerts → the dispatch is reaching GitHub; move
     to step 2.
2. **GitHub → Actions tab → "Watch files in external repo".**
   - **No runs** since the outage → the dispatch isn't arriving (cron-job.org or token);
     stay on step 1.
   - **Failed runs** → the workflow itself is erroring; open the failing run's logs.
   - **Successful runs, no Telegram** → check `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`
     secrets, or there were simply no new listings.
3. **Verify the PAT** at GitHub → Settings → Developer settings → Personal access tokens —
   check its **expiry** and that it still grants `Actions: write` on `kiankian/repo-watcher`.

## Application tracker (Telegram → Google Sheet)

Each new job alert includes an inline `✅ Applied` button. Tapping it appends a row to a Google Sheet and edits the Telegram message to confirm.

The same workflow (`watch-files.yml`) runs both jobs in order:

1. `watch` — checks the upstream repos and sends per-job alerts with inline buttons. Pending jobs (those waiting for an Applied tap) are recorded in `.bot_state.json`.
2. `process_applies` — polls Telegram `getUpdates`, looks up tapped jobs in `.bot_state.json`, appends a row to the sheet, and marks the message as logged.

### Required repo secrets

| Secret | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot auth — same token used to send alerts and read callback queries. |
| `TELEGRAM_CHAT_ID` | Chat that receives alerts. |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full service account JSON (paste the whole file as the secret value). |
| `APPLICATIONS_SHEET_ID` | The spreadsheet ID from its URL (`docs.google.com/spreadsheets/d/<ID>/edit`). |
| `HEALTHCHECK_PING_URL` | *Optional.* External dead-man switch pinged after each successful run (see Logs and health). |

Optional repo variable (not secret):

| Variable | Default | Purpose |
|---|---|---|
| `APPLICATIONS_SHEET_RANGE` | `Applications!A:F` | Tab + range to append to. |

### Google Sheet setup

1. Create a sheet. Add a tab named `Applications` (or set `APPLICATIONS_SHEET_RANGE`).
2. Add a header row matching the schema below.
3. Create a GCP service account, enable the Google Sheets API, download its JSON key, and paste the whole JSON into the `GOOGLE_SERVICE_ACCOUNT_JSON` secret.
4. Share the sheet with the service account's email (`...@...iam.gserviceaccount.com`) as **Editor**.

Sheet schema (what the bot writes per applied tap):

| A: Company | B: Role | C: Date Applied | D: Email Applied | E: Resource | F: Status |
|---|---|---|---|---|---|
| from alert | from alert | UTC `M/D/YYYY` | _(blank — fill manually)_ | `Telegram Bot` | `Applied` |

Make sure your **Status** dropdown includes `Applied` as a valid option, or Sheets will flag the row.

### Bot setup (one-time)

The bot uses long-polling via `getUpdates` — **do not** set a webhook on the bot, or `getUpdates` will fail with HTTP 409. If you previously set one, clear it:

```sh
curl "https://api.telegram.org/bot<TOKEN>/deleteWebhook"
```

## Tests

Pure parsing and dedup logic lives in `watcher/core.py`; the workflow imports it and keeps only its I/O. `tests/conftest.py` extracts the python block straight out of `watch-files.yml` and runs it against a temporary checkout with `urlopen` stubbed, so the end-to-end tests drive the same code that runs in CI rather than a reimplementation.

```sh
pip install pytest
python -m pytest -q
```

`.github/workflows/tests.yml` runs on pushes and PRs that touch `watcher/`, `tests/`, or either workflow. It is path-filtered deliberately: the watcher pushes a state commit most minutes, and without filters every one of them would start a test run.

Fixtures under `tests/fixtures/` are frozen copies rather than the live state files, which the runner rewrites continuously. `collapse_84.json` / `collapse_28.json` are the real snapshots either side of the 2026-07-30 01:37 UTC row collapse, replayed as a regression test.

## Pushing a local commit when the remote has advanced

Because the workflow pushes `.watcher_state.json` updates on every external cron-job.org trigger, you will frequently find that the remote has moved ahead of your local branch. If you try to push on top of that, the branches will appear divergent.

**Always rebase your local commits on top of the remote before pushing:**

```sh
git pull --rebase origin main
git push origin main
```

This places your local commits on top of the automated state-update commits from the runner, keeping a linear history.

### Avoid

- `git pull` without `--rebase` — creates noisy merge commits that interleave with the automated state updates.
- `git push --force` — will overwrite the watcher's state commits and cause the next run to re-alert on stale diffs.
- Resolving "divergent branches" errors by `git reset --hard` — discards in-flight work.

### If a rebase conflicts on `.watcher_state.json`

Keep the **remote** version (the runner's SHA is more recent than yours):

```sh
git checkout --theirs .watcher_state.json
git add .watcher_state.json
git rebase --continue
```


# TODO
- Simplify repos: **SWE-only is intentional** — Product Management, Data Science/AI/ML,
  Quant Finance, and Hardware sections are deliberately not watched. (Verified: within the
  watched SWE sections, every job row is parsed; the only skipped `<tr>` is the table header.)
- [ ] Other things:
    - [x] Connect to google sheet (`process_applies` job — appends a row on each `✅ Applied` tap)
    - [ ] Text to update
- [ ] More job boards:
    - [x] https://github.com/speedyapply/2027-SWE-College-Jobs — **Speedyapply Summer Repo** (USA SWE Internships; all 3 tables — FAANG+/Quant/Other)
    - [x] https://github.com/zapplyjobs/Internships-2027 — **Zapply Summer Repo** (Software Engineering table only)


- Spreadsheet URL: 1UHDefi6XPSs7sypXMmAIWsCuoE_UswBCipDMbgisX5w