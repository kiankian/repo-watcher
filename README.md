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
identity = (apply_url or NOURL) |company|role|location|term #occurrence
```

A listing is alerted when its identity has never been delivered. Four properties make that safe:

1. **`seen` only grows.** It is a union, never a replacement. When the parsed row set shrinks — a truncated parse, or upstream pruning its table — the rows that vanished stay in `seen`, so they do not re-alert when they come back, while anything genuinely new in the same run still goes out.
2. **An identity is recorded only after Telegram confirms the message, and undelivered work is queued durably.** A failed or rate-limited send — or a batch over `BURST_CAP` — is written to that source's `outbox` as a full `[row, identity, occurrence]` triple and drained on subsequent runs, including runs where the upstream SHA has not changed. Withholding from `seen` alone was not enough: the retry re-derived the row from a fresh parse, so anything that left the upstream table in the meantime was lost. Zapply's table re-sorts and is capped at ~100 rows, and a long delivery outage widens that window arbitrarily.
3. **Every parsed row gets exactly one identity, and no two rows in a run share one.** Every field participates, because either half alone collapses distinct openings. URL alone is not enough: boards sometimes publish a generic link shared by several rows, and if one of those openings is replaced while the row count stays the same, occurrence numbering hands the replacement an already-seen identity. Text alone is not enough either: Copart posts several Dallas SWE-intern reqs differing only by Workday ID. `term` also lets a requisition relisted for a new season through, and the occurrence index separates rows identical in every field (Kudu Dynamics lists the same URL-less role three times).

4. **A run cannot alert a whole table at once.** Because the identity is URL-first, an upstream generator that stops emitting apply URLs re-keys every row in a single commit, and every listing on the board looks new. What distinguishes that from real news is not the rows but the shape of the run: real listings arrive by being added upstream, so discoveries come with a row count that rose to match, while a re-key mints identities for rows that were already there and the count does not move. Discoveries that the table's own growth does not explain are counted, and at `IDENTITY_RESET_MIN` (25) or more the run is treated as a fault — the discoveries are dropped unsent and unrecorded, `⚠️ identity-reset` goes out, and the SHA is held so the recovery commit is re-parsed rather than skipped. Nothing is lost: the rows are still listed upstream, so they are re-derived once upstream is consistent and delivered then. See [Whole-table re-key](#whole-table-re-key).

Including the text costs a duplicate whenever upstream edits a role or location string in place. Measured across 2,060 state snapshots spanning 18 days and 475 distinct `(source, URL)` pairs, that happened **zero** times — so the protection is effectively free.

Failure modes that remain all produce a *duplicate*, never a miss: a URL gaining or losing tracking parameters, URL-less rows being reordered, or `SEEN_CAP` eviction (logged when it happens). The one remaining way to lose a job outright is `OUTBOX_CAP` overflow, which requires delivery to have been failing for roughly 40 consecutive runs and is alerted loudly.

State lives in `.watcher_state.json`, one entry per source:

```json
{ "<state_key>": { "last_sha": "...", "seen": ["<identity>", ...],
                   "seen_legacy_urls": ["<url>", ...], "last_row_count": 28,
                   "outbox": [[[company, role, location, term, url], identity, occurrence]] } }
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
| `logs/runs-YYYY-MM.jsonl` | watcher per run that did something, plus an hourly heartbeat | `{ts, run_id, watcher, state_key, prev_sha, latest_sha, rows_extracted, prev_row_count, seen_size, queued_before, identities_new, sent_ok, sent_failed, outbox_size, skip_reason}` |

The alert log exists because `.bot_state.json` cannot serve as one: `pending` is a dict keyed by job hash, so a re-sent alert **overwrites its own record** and the evidence disappears. Reconstructing past duplicates meant diffing gaps in Telegram message IDs; these logs make it a one-line query. The run log turns a shrinking parse into a visible time series rather than something only findable by diffing state commits.

Quiet runs are collapsed into the hourly heartbeat on purpose — at one dispatch a minute, logging unconditionally would be ~8,600 lines a day.

### Sequence numbers

Each alert carries a number (`Simplify Summer Repo #1032`), committed only after Telegram confirms the send. `process_applies` preserves it when it edits a message to `✅ Logged`.

**What it proves, precisely:** no message that was sent *and recorded* is missing from your chat. Because the number is allocated after delivery, a job lost to parsing, dedup, or a failed retry never consumes one — so it leaves no gap. A gap therefore means a delivered message was removed or state was rolled back; it is **not** a detector for missed discoveries.

For that, use the two durable records below.

### Detecting a missed job

Misses are caught by reconciling what was *observed* against what was *delivered*, not by the numbering:

| Signal | Where | Means |
|---|---|---|
| `outbox_size` not trending to zero | `logs/runs-*.jsonl`, and `outbox` in `.watcher_state.json` | jobs were observed as new but never delivered — the queue is stuck |
| `⚠️ outbox-overflow` | Telegram | the queue exceeded `OUTBOX_CAP` and jobs were dropped. This is a real miss |
| `⚠️ fetch-failed` / `⚠️ zero-rows` | Telegram, and `skip_reason` in the run log | a source produced nothing; anything posted there while it was broken was never seen |
| `⚠️ identity-reset` | Telegram, and `skip_reason=identity_reset:<n>` in the run log | a source re-keyed its whole table; its discoveries were dropped unsent. Not a miss on its own — they redeliver once upstream is consistent — but the source is blind until then. See [Whole-table re-key](#whole-table-re-key) |
| `queued_before + identities_new` vs `sent_ok + sent_failed` | `logs/runs-*.jsonl` | should always match. `identities_new` counts only fresh discoveries, so `queued_before` is needed to balance a backlog drain |
| `outbox_size` vs `sent_failed` | `logs/runs-*.jsonl` | should match. `outbox_size` is always the depth *after* the run, on every code path |
| pings stopped | your healthcheck provider | either the dispatch died or a source is unreadable (see below) |

The `outbox` is the important one: every job observed as new is persisted there *before* delivery is attempted and removed only once Telegram confirms it, so a job cannot be quietly dropped between discovery and delivery. It drains on every run, including runs where the source could not be fetched or parsed — the queued triples are self-contained, so a permanent upstream rename must not strand jobs that were already discovered.

### Health alerts

Operational faults are sent to the same chat with a `⚠️ watcher:` prefix, rate-limited to one per kind per 30 minutes:

- a source could not be **fetched** at all (the watched file was renamed, moved or deleted)
- a source parsed **0 rows** (renamed heading or reshaped table — otherwise indistinguishable from "no new listings")
- the outbox overflowed `OUTBOX_CAP`, dropping undelivered jobs
- a parse returned under 70% of its previous row count
- a source discovered `IDENTITY_RESET_MIN` (25) more listings than its row-count growth explains — the whole-table re-key breaker, below
- a send failed (the listing stays unrecorded and will be retried)
- `SEEN_CAP` eviction
- no successful run for over 2 hours

> ⚠️ **The 2-hour silence check can only fire during a run that actually happens.** It catches the workflow erroring, or the dispatch stalling and recovering — it *cannot* detect the dispatch stopping for good, which is the most likely outage (see below). Closing that gap needs an external dead-man switch, and one is now configured: the `HEALTHCHECK_PING_URL` secret holds a [healthchecks.io](https://healthchecks.io) ping URL, and the workflow hits it as its final step — leaving that service to notify you when the pings stop, with no run required. Settings and triage are below. Unset, the ping is skipped.

The ping deliberately runs **after** the state push, and is skipped if any earlier step failed. It has to mean "this run completed *and* persisted", not "the python finished": if the push exhausts its retries after alerts went out, those identities were never recorded and will resend, so that run is not healthy.

It is **also** withheld when any source was unreadable — a failed fetch or a zero-row parse. A source whose file was renamed produces no alerts at all, so continuing to ping would hide that silence behind a green check, which is the exact outage this switch exists to surface. Note this means a persistently broken source keeps the pings stopped until it is fixed; the accompanying `⚠️` message says which source. The `last_ok_run` timestamp behind the 2-hour silence alarm is deliberately *not* gated this way, since it tracks whether the dispatch pipeline is alive — conflating the two would later produce a bogus "no successful run for Nh" about runs that did happen.

### The external dead-man switch, as configured

Live on [healthchecks.io](https://healthchecks.io) since 2026-07-30, notifying by **email and Telegram**.

| Setting | Value |
|---|---|
| Period | **5 minutes** — the expected gap between pings |
| Grace | **55 minutes** — how long a late check waits before alerting |
| Time to alert | **≈ 1 hour** from the last healthy run |

**The knob is the total, not the split.** The watcher pings roughly once a minute, so the 5-minute period is just slack for a slow run, a run queued behind the `repo-watcher-state` concurrency group, or a push that needed a retry. One hour was chosen so that (a) an hour with zero healthy runs is unambiguous rather than noise, and (b) it fires *before* the in-band 2-hour silence check can, so the out-of-band switch is what tells you first. Shorten it if you would rather know sooner and can tolerate false alarms, but not below ~15 minutes.

**Two channels on purpose.** healthchecks.io is external, so it can still reach you when the watcher is dead — but if the outage *is* Telegram, only email gets through. The failure modes are independent, so neither channel alone is sufficient.

**It alerts once.** healthchecks.io notifies on state *transitions*, not on a timer: one message when the check goes down, one if it later recovers. There is no re-nagging, so a missed notification is missed for good. That is the main limitation of this setup and the other reason for two channels.

Treat the ping URL as a credential — anyone holding it can forge "I'm alive" pings and suppress the alert. It is stored as the `HEALTHCHECK_PING_URL` repository secret and masked in logs. To rotate it, delete the check and recreate it with the settings above; the UUID is regenerated.

**Verifying it after any change** — all three, because a switch you have not tested is worse than none, since you will trust it:

1. A real run's `Ping healthcheck` step is green **and its log shows `OK`**. Green alone is not proof: the step is `curl … || echo`, so a failed ping still passes, and with the secret unset the whole thing is a no-op that also passes. The log line `HEALTHCHECK_PING_URL: ***` (masked, therefore non-empty) followed by `OK` is the actual evidence.
2. A dry run's `Ping healthcheck` step is **skipped**, proving a rehearsal cannot masquerade as a healthy run.
3. A throwaway check — period 1m, grace 1m, same channels, never pinged — produces a real notification on every channel, then delete it. Do **not** test this by breaking the watcher. Note that opening the ping URL in a browser *is* a ping and resets the timers. "Send Test Notification" proves a channel is wired but not that a missed ping alerts.

#### When it fires, read it correctly

The notification says only "no ping received". It does not say why, and the causes need opposite responses:

| Actions tab shows | Cause | Do |
|---|---|---|
| Recent runs, green | **A source broke.** Runs are fine; `healthy` came back `false` so the ping was withheld | Check Telegram for `⚠️ zero-rows` or `⚠️ fetch-failed` — it names the source. Fix the section marker or parser |
| Recent runs, **`watch` red** | That job is failing — commonly the state push exhausting its retries | Open the failing run. The ping is correctly withheld: alerts may have gone out unrecorded |
| Nothing since the last ping | **Dispatch stopped.** cron-job.org disabled, or its PAT expired | The runbook below |

The first row is the likelier one — upstream repos get reorganised regularly — so check the Actions tab *before* assuming the watcher is dead. Only `fetch-failed` and `zero-rows` set `healthy=false`; a `⚠️ shrink` warning does **not** withhold the ping, so it will never be the cause of a healthcheck alert on its own.

> **A red run is not always a withheld ping — check which job is red.** `Ping healthcheck` is the last step of `watch`; `process_applies` is a separate job that starts only after `watch` has finished and already pinged. So a run that is red *because `process_applies` failed* — a Google Sheets append, a Telegram edit, its own `.bot_state.json` push — still pinged, and no dead-man alert will ever fire for it.
>
> **The callback path is therefore not covered by this switch, by design.** Gating the ping on both jobs would mean a Sheets hiccup raising a "the watcher is dead" alert while job alerting is perfectly healthy — a false alarm on the highest-severity channel for a much lower-severity fault, which is how you teach yourself to ignore it. The tradeoff is that a persistently failing `process_applies` (revoked service account, deleted spreadsheet) is silent apart from red runs in the Actions tab: taps stop reaching the Sheet and buttons stop being ticked, while alerts keep arriving normally. A transient failure re-reads the tap on the next run rather than losing it, since `last_update_id` only advances once the job commits — but **the retry is not clean.** Within one tap the order is `append_row` → `answerCallbackQuery` → `editMessageText` → offset. A failure after the append but before the commit lands leaves the row in the Sheet while `pending` still holds the hash, so the retry appends it a second time. Expect duplicate Sheet rows after any red `process_applies`, and check the Sheet rather than assuming the retry cleaned up after itself. Closing both this and the persistent case needs its own signal — see `FUTURE_IMPROVEMENTS.md`.

### Dry runs

Run the workflow from the Actions tab with the dry-run box checked to parse live upstream data, compute identities, and print what *would* be sent — without sending a message, writing state, or committing. Use it to rehearse a change before any alert can go out.

**Finding the checkbox.** The input is named `dry_run` in the workflow, but GitHub labels a boolean input with its *description*, so nothing in the form is called `dry_run`. The control is the checkbox next to:

> ☐ Parse and report only: send no Telegram messages and write no state.

Leaving it unticked is a live run that sends and commits, so the tick is the whole safety margin.

> **Dispatch it from the default branch.** A run from any other ref is forced into dry mode and writes nothing, on purpose: it would carry that branch's stale state snapshot and re-alert jobs you already have, and it could not save what it sent, because the commit step targets the default branch and conflicts against a shallow divergent checkout. Note also that GitHub builds the "Run workflow" form from the default branch, so for a branch that has not been merged yet the checkbox does not appear at all — which is how a branch dispatch once went out as a real run and sent three unrecorded alerts.

A dry run deliberately **ignores the unchanged-SHA short-circuit** that a normal run uses, and re-fetches every enabled source. It has to: the watcher stores the current head on every run, so a rehearsal launched a minute later would find every source unchanged, skip all of them, and report "no new listings" without having parsed anything — silently passing whatever parser or config edit it was meant to validate. The per-run summary table shows `rows` extracted per watcher, which is what tells you the extraction still works.

The `process_applies` job is skipped entirely on a dry run. It has to be: it appends to the Google Sheet, edits Telegram messages and commits `.bot_state.json`, so leaving it to run would make a rehearsal mutate external state.

### If alerts stop (troubleshooting runbook)

Because nothing in *this* repo triggers the watcher, a silent stop is almost always on the
cron-job.org / token side. Diagnose in this order:

> If you arrived here from a **healthchecks.io** alert, check the triage table above first. This
> runbook covers the dispatch-stopped case; a broken source is the likelier trigger and needs a
> different fix.

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

### Whole-table re-key

The opposite failure: instead of going quiet, one source alerts its entire board at once.

This happened on **2026-08-01 01:56 UTC**. Upstream commit `zapplyjobs/Internships-2027@8eb9fd34`
replaced every apply URL in the README with the placeholder `#` — 499 of them, the whole file,
not just the watched section. The identity is URL-first, so all 100 rows in the Software
Engineering table re-keyed at once, `select_new` correctly reported 100 discoveries, and the run
alerted 25 (`BURST_CAP`) and queued 75. The next two runs drained 25 each. Every one of those
messages carried a `#` where the apply link belongs, so none of them were actionable. No existing
guard caught it: the fetch worked, the section markers matched, and the parse returned exactly
100 rows as always — only the *contents* of one column had degraded.

`IDENTITY_RESET_MIN` now stops this (see [Delivery guarantee](#delivery-guarantee), property 4).
If it fires:

1. **Read the alert.** It names the source, the discovery count, and the row counts either side.
2. **Open the upstream table** and compare a row against `PARSING_REFERENCE.md`. Look for an
   apply-URL column that has gone blank or become a placeholder, a reordered or inserted column,
   or a heading change that shifted the section slice.
3. **If upstream is broken**, do nothing. The breaker re-arms every run, holds the SHA, and
   resumes delivery by itself once upstream is consistent. Genuinely new listings that appeared
   during the outage are still in the upstream table and go out on the first healthy run.
4. **If upstream changed shape deliberately**, fix the column indexes in `WATCHERS` — the
   breaker is telling you the parser config is stale. Rehearse with `dry_run` before merging.
5. **Only if the table legitimately turned over** (a season rollover republishing everything
   under new URLs) is the suppression unwanted. Re-run after the breaker clears, or raise
   `IDENTITY_RESET_MIN` for that rollover and put it back afterwards.

Note the breaker suppresses *discoveries*, not the queue: anything already in the `outbox` was
vetted on the run that found it and keeps draining while the breaker holds.

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
| `HEALTHCHECK_PING_URL` | *Optional, and currently set.* External dead-man switch pinged after each successful run — must be a **repository** secret, since the step declares no environment. See [The external dead-man switch, as configured](#the-external-dead-man-switch-as-configured). Unset, the ping step is a no-op. |

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
pip install pytest pyyaml
python -m pytest -q
```

See [`TESTING.md`](TESTING.md) for what each guarantee rests on, how to check parsers against live data without sending anything, and the traps that have made tests here pass for the wrong reason.

`pyyaml` is needed by `tests/test_workflow_yaml.py`, which parses `watch-files.yml` to assert on the job- and step-level `if:` guards that Actions evaluates and the python harness cannot reach. Without it, collection aborts before any test runs.

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
