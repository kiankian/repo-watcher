# Contributor Guide

## Repository purpose

This repository is a small, stateful GitHub Actions application. It polls upstream
internship-listing repositories, parses selected tables, sends new listings to
Telegram, and lets a user mark an alert as applied so it can be appended to a
Google Sheet.

The application intentionally lives almost entirely in
`.github/workflows/watch-files.yml`: the workflow contains two inline Python
programs and the shell used to commit updated state. There is no separate package,
build system, or test suite.

## Read these files first

- `README.md` documents setup, external triggering, secrets, operations, and the
  outage runbook.
- `PARSING_REFERENCE.md` documents upstream formats, section boundaries, parsing,
  listing identity, and deduplication behavior.
- `.github/workflows/watch-files.yml` is the executable source of truth. Its
  `WATCHERS` list defines all sources and parser configuration.
- `.watcher_state.json` and `.bot_state.json` are runtime data committed by the
  workflow. Inspect them when necessary, but do not hand-edit, reformat, reset, or
  include incidental changes to them in feature commits.

## Architecture and data flow

The workflow is manually dispatchable only. An external cron-job.org request
dispatches it every minute; **do not add a GitHub Actions `schedule` trigger**.
Concurrency is serialized because both jobs write committed state.

1. The `watch` job fetches each enabled upstream branch SHA and skips unchanged
   sources that already have initialized state.
2. It fetches the README at that exact SHA, slices the configured section, and
   parses HTML or Markdown rows into
   `[company, role, location, term_or_category, apply_url]`.
3. Snapshot sources compare `(company, role, location, term)` against the prior
   row snapshot. Zapply and Speedyapply instead use an append-only, capped set of
   apply URLs because their table behavior makes snapshot identity unreliable.
4. New jobs are sent as individual Telegram messages. Pending callback metadata
   is saved in `.bot_state.json`.
5. The `process_applies` job reads Telegram callback updates, appends applied jobs
   to Google Sheets, edits the Telegram message, and advances bot state.
6. Each job commits its state changes and rebases before pushing.

There are eight configured watcher entries. The two off-season entries are
currently disabled; three Speedyapply entries represent separate tables but share
one user-facing label.

## Non-negotiable behavior

- Preserve `workflow_dispatch` as the only trigger. Scheduling is external by
  design.
- Preserve silent bootstrap: initializing a source must seed state without
  alerting every existing listing.
- Preserve empty-parse guards. Marker or format drift must not overwrite valid
  state or advance the stored SHA.
- Do not broaden category scope accidentally. Simplify is SWE-only, Vansh uses
  its full uncategorized list, Zapply is SWE-only, and Speedyapply watches all
  three USA internship tables.
- Keep query strings in apply URLs; some ATS systems encode the job ID there.
- Keep URL-based cumulative dedup for Zapply and Speedyapply unless the upstream
  identity model is deliberately redesigned and migrated.
- Do not add age, salary, or other volatile display fields to snapshot identity.
- Preserve the `↳` continuation rule, which inherits the previous company.
- Never print, commit, or place real Telegram, GitHub, Google service-account, or
  spreadsheet credentials in fixtures or documentation.
- Never force-push or discard newer state commits. The workflow advances the
  remote frequently, so rebase local work on the remote before pushing.

## Making changes

- Keep the inline Python dependency-light; the watcher uses the standard library,
  while callback processing pins `google-auth` in the workflow.
- Never wrap imports in `try`/`except` blocks.
- Prefer small, explicit parser configuration over label-specific conditionals.
- Treat section markers and column indexes as an upstream API. Check them against
  representative current source content before changing them.
- If parsing or watcher scope changes, update both `README.md` and
  `PARSING_REFERENCE.md` in the same commit. The workflow remains authoritative
  if prose and code disagree.
- If state shape changes, include backward-compatible migration logic and explain
  bootstrap/re-alert implications in `PARSING_REFERENCE.md`.
- Avoid unrelated formatting of the large JSON state files.

## Validation

There is no live end-to-end test that is safe to run locally: executing the full
inline programs can call GitHub, Telegram, and Google Sheets and mutate production
state. Prefer static checks and isolated parser fixtures.

Run these checks after relevant changes:

```sh
# Basic workflow syntax (Ruby is available on GitHub-hosted runners).
ruby -e 'require "yaml"; YAML.parse_file(".github/workflows/watch-files.yml"); puts "YAML parses OK"'

# Validate committed runtime JSON without rewriting it.
python3 -m json.tool .watcher_state.json >/dev/null
python3 -m json.tool .bot_state.json >/dev/null

# Check whitespace errors and inspect the exact patch (including accidental state changes).
git diff --check
git status --short
git diff -- . ':(exclude).watcher_state.json' ':(exclude).bot_state.json'
```

When editing inline Python, extract each `python - <<'PY'` heredoc to a temporary
file and run `python3 -m py_compile` on it. For parser changes, use small local
fixtures containing headers, separators, continuation rows, missing markers,
empty tables, HTML entities, Markdown links, and URLs with query strings. Do not
invoke Telegram or Sheets as part of validation.

## Git and review hygiene

- Before pushing, use `git pull --rebase origin main`; never use a merge pull or
  `git push --force`.
- If `.watcher_state.json` conflicts during a rebase, retain the newer remote
  runner state as described in `README.md`.
- Keep commits focused. In the pull request, summarize behavioral changes,
  affected sources/state shapes, documentation updates, and exact validation
  commands. Call out checks not run because they require production secrets.
