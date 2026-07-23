# repo-watcher

A GitHub Actions workflow that watches job-board repos and sends Telegram alerts when new listings appear.

Currently watches **6 sources across 4 repos** (see the `WATCHERS` list in `.github/workflows/watch-files.yml` for the source of truth — Speedyapply is implemented as 3 entries, one per category table, sharing one label):

| Watcher label | Repo | Branch | File | Scope |
|---|---|---|---|---|
| Simplify Off-Season Repo | `SimplifyJobs/Summer2026-Internships` | `dev` | `README-Off-Season.md` | Software Engineering section only |
| Simplify Summer Repo | `SimplifyJobs/Summer2026-Internships` | `dev` | `README.md` | Software Engineering section only |
| Vansh Off-Season Repo | `vanshb03/Summer2027-Internships` | `dev` | `OFFSEASON_README.md` | full listing table |
| Vansh Summer Repo | `vanshb03/Summer2027-Internships` | `dev` | `README.md` | full listing table |
| Zapply Summer Repo | `zapplyjobs/Internships-2027` | `main` | `README.md` | Software Engineering section only (cumulative-URL dedup) |
| Speedyapply Summer Repo | `speedyapply/2027-SWE-College-Jobs` | `main` | `README.md` | USA SWE Internships — all 3 tables: FAANG+, Quant, Other (cumulative-URL dedup) |

The two **Simplify** watchers intentionally parse only the `## 💻 Software Engineering Internship Roles` section — Product Management, Data Science/AI/ML, Quant Finance, and Hardware roles are excluded by design. The two **Vansh** watchers parse the entire `## The List` table, which is uncategorized (so non-SWE roles do flow through from those sources). The **Zapply** watcher parses only the `💻 Software Engineering` table of `zapplyjobs/Internships-2027`; its other five category tables (Data Science & AI, Hardware & Engineering, Product/Design/Research, Business & Operations, Other) are excluded by design. The **Speedyapply** source (`speedyapply/2027-SWE-College-Jobs` → `README.md`) watches the **USA SWE Internships** page in full — all three of its category tables (FAANG+, Quant, Other) — via three watcher entries that share the single "Speedyapply Summer Repo" label and stamp the category into the alert's last field; the repo's separate New-Grad and International pages (`NEW_GRAD_USA.md`, `INTERN_INTL.md`, `NEW_GRAD_INTL.md`) are not watched.

State is persisted in `.watcher_state.json`. Most sources store a `{last_sha, rows}` snapshot and alert on the diff against the previous snapshot. **Zapply and Speedyapply are the exceptions:** each stores `{last_sha, seen: [apply_url, ...]}` — a cumulative, capped (`URL_CAP`) set of every apply URL ever seen — and alerts a URL only the first time it appears. For **Zapply**, this is because its table re-sorts and is capped at ~100 rows every ~15 min, so listings flap in and out of the visible window and a snapshot diff would re-alert on every re-add; keying on the apply URL (the only stable, unique field — Role/Location are truncated with `...` and Posted is the constant `Recently`) avoids that. For **Speedyapply**, many genuinely-distinct openings share the same company + role + location and differ only by apply URL (e.g. Copart lists five identical-looking Dallas SWE-intern rows with different Workday IDs), so the snapshot key `(company, role, location, term)` would collapse them and miss real additions; keying on the apply URL alerts each opening exactly once. Both keep the full query string intact, since some ATS job IDs live there (e.g. Greenhouse `?gh_jid=`). The workflow commits the state file back to this repo on every run that advances a SHA.

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
    - [x] https://github.com/speedyapply/2027-SWE-College-Jobs — **Speedyapply Summer Repo** (USA SWE Internships; all 3 tables — FAANG+/Quant/Other; cumulative-URL dedup)
    - [x] https://github.com/zapplyjobs/Internships-2027 — **Zapply Summer Repo** (Software Engineering table only; cumulative-URL dedup)


- Spreadsheet URL: 1UHDefi6XPSs7sypXMmAIWsCuoE_UswBCipDMbgisX5w