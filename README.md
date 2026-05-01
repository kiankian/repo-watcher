# repo-watcher

A GitHub Actions workflow that watches job-board repos and sends Telegram alerts when new listings appear.

Currently watches:

- `SimplifyJobs/Summer2026-Internships` — `README-Off-Season.md` on `dev`
- `vanshb03/Summer2027-Internships` — `OFFSEASON_README.md` on `dev`

State is persisted in `.watcher_state.json` (last-seen SHA per repo). The workflow commits that file back to this repo on every run that advances a SHA.

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
| from alert | from alert | UTC `YYYY-MM-DD` | _(blank — fill manually)_ | `Telegram Bot` | `Applied` |

Make sure your **Status** dropdown includes `Applied` as a valid option, or Sheets will flag the row.

### Bot setup (one-time)

The bot uses long-polling via `getUpdates` — **do not** set a webhook on the bot, or `getUpdates` will fail with HTTP 409. If you previously set one, clear it:

```sh
curl "https://api.telegram.org/bot<TOKEN>/deleteWebhook"
```

## Pushing a local commit when the remote has advanced

Because the workflow pushes `.watcher_state.json` updates on its own schedule, you will frequently find that the remote has moved ahead of your local branch. If you try to push on top of that, the branches will appear divergent.

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
- [ ] FIX SIMPLIFY REPO
    - [ ] Missing a lot of new jobs
    - [ ] ONLY SWE ROLES
- [ ] Other things:
    - [ ] Connect to google sheet
    - [ ] Text to update
- [ ] More job boards:
    - [ ] https://github.com/speedyapply/2026-SWE-College-Jobs
    - [ ] https://github.com/zapplyjobs/Internships-2026#-software-engineering


- Spreadsheet URL: 1UHDefi6XPSs7sypXMmAIWsCuoE_UswBCipDMbgisX5w