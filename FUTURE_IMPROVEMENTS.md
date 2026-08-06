# Future Improvements

Ranked by usefulness divided by effort — do them roughly top-down.

Before adding to this list, check "Already shipped" at the bottom. Half of the obvious ideas for
this project landed in #1 and #2, and a roadmap that re-proposes finished work is worse than no
roadmap.

---

## Open

### 1. Bound `.bot_state.json` growth

`pending` only ever grows. Entries leave it when you tap `✅ Applied` (`watch-files.yml:1136`) and
in no other way. Currently **1,094 pending against 33 applied, 524 KB**, rewritten and committed on
most runs — roughly every 5 minutes since the cron was slowed on 2026-08-06.

Pausing the tracker that same day does not change this materially: at 33 taps against 1,094 entries
the drain was removing ~3% of what the alert path adds, so the growth rate is essentially the alert
rate either way. It does remove the only mechanism that shrinks the file, so if this is ever fixed
properly, do it independently of whether the tracker is running.

Nothing is broken today (the `.git` pack is ~2 MB), but it accretes ~120 entries/day forever, and
every one of those is a full-file rewrite in a new commit. Left alone this is a slow repo-bloat
problem that eventually costs checkout and push time on every run.

Suggested fix: expire `pending` entries older than N days (60?) on write, capped like `seen` is.
The only cost of expiring one is that tapping a very old `✅ Applied` button stops working —
`process_applies` resolves a tap by looking up its hash, so an expired entry means the tap is
ignored. Log expiries so that is diagnosable rather than mysterious.

**Add a test first.** This touches the callback path, which `test_job_hash_matches_the_hashes_in_bot_state`
guards for a reason: every button already in the chat carries a hash.

### 2. Re-enable the off-season watchers when their season opens

`SimplifyJobs/Summer2026-Internships` and `vanshb03/Summer2027-Internships` are
`"enabled": False` in `WATCHERS`. This is a calendar item, not an engineering one, but forgetting it
means two sources produce zero alerts and *nothing reports that* — a disabled watcher is
indistinguishable from a quiet one.

Their state is intact (539 and 180 identities, last parsed at their pause SHAs), so flipping the
flag resumes without re-alerting. Worth a calendar reminder rather than a code change.

### 3. Give the callback path its own failure signal

`process_applies` is the only part of the system with no health signal at all. It is a separate job
from `watch`, and `Ping healthcheck` is `watch`'s final step — so `process_applies` starts *after*
the ping has already gone out. A run that is red purely because this job failed still pinged, and
the dead-man switch will never fire for it.

That is the right design (see the note under "The external dead-man switch, as configured" in
`README.md`): folding it into the ping would mean a Google Sheets hiccup raising a "the watcher is
dead" alert while job alerting is fine. But it leaves a real gap. A revoked service account or a
deleted spreadsheet stops taps reaching the Sheet and stops buttons being ticked, indefinitely and
silently — job alerts keep arriving normally, so nothing looks wrong.

A transient failure does not lose the tap: `advance_offset` runs per update only after that tap's
append and edit succeed, and `.bot_state.json` is written after the loop and committed in a later
step, so any failure leaves `last_update_id` where it was and the tap is re-read next run.

**But the retry is not idempotent, and the offset is not the thing that guards it.** Per tap the
order is `append_row` → `answerCallbackQuery` → `editMessageText` → mutate `applied`/`pending` →
`advance_offset`. The two *expected* failures are handled cleanly — `sheets_credentials()` and
`append_row` both catch and `break` before anything is mutated. Everything after a successful
append is not: an uncaught error in either `tg_call`, or the `Commit updated bot state` step
exhausting its five push retries, leaves the Sheet holding the row while `pending` still holds the
hash. The next run re-reads the tap, finds it in `pending`, and appends a **second** row. The push
case duplicates every tap processed in that run, not just one.

Two failures compound this. The window is unbounded in one direction — Telegram retains
undelivered `getUpdates` results for **24 hours**, so a failure persisting past that loses every
tap made during it permanently, with no record anywhere. And in the other direction the retry
itself creates duplicates. So the current behaviour is: transient → duplicate Sheet rows;
persistent → silent permanent loss.

The `⚠️` signal above addresses the second and makes the first visible, which is why it comes
first. Genuine idempotency is a separate, larger change: writing the job hash into a Sheet column
and skipping an append whose hash is already present. Worth it only if duplicates actually start
appearing — taps are manual and low-volume (33 logged to date), so the cheap fix is to notice and
delete the duplicate row. Do **not** "fix" this by advancing the offset before the append; that
converts a visible duplicate into a silent lost tap.

Cheapest fix: an `if: failure()` step on `process_applies` that sends a `⚠️` Telegram message
naming the failed step. It reuses the channel already trusted for operational faults and needs no
new infrastructure. Rate-limit it the way `health_alert` does — this job runs on every dispatch,
and an unthrottled failure notice would send ~290 messages a day at the current 5-minute cron
(~1,400 under the old 1-minute one). Note it is paused behind `PROCESS_APPLIES` as of 2026-08-06,
so this is only worth building when the tracker comes back.

### 4. Validate state shape on load, and document recovery

`state = json.loads(STATE_FILE.read_text())` at `watch-files.yml:490` has no guard. A truncated or
malformed commit raises, which **fails safe** — the run dies before writing anything, no alerts go
out, no ping fires, so you find out. That is the right failure mode and worth keeping.

What is missing is the other half: a check that the *shape* is sane (each entry has the five
expected keys, `seen` is a list of strings, `outbox` entries are 3-element), and a documented
restore path.

**A plain `git revert` of `.watcher_state.json` is not that restore path.** Corruption is usually
noticed after later runs have already delivered. Reverting to a good commit drops every identity
recorded since it from `seen`, and those jobs re-alert on the next run if they are still listed
upstream. Nothing repairs this automatically: `migrate_state` returns any entry already carrying
`seen_legacy_urls` untouched (`watcher/core.py`), so the bot records do *not* reseed the gap the way
they did at the 2026-07-30 cutover. The revert also restores that commit's `outbox`, re-queuing
triples that have since been delivered.

The recovery to write down is **additive**, because `seen` is append-only and every identity
removed from it is a duplicate alert:

- `seen` — union of the good commit's set with everything delivered since. That record exists and
  is append-only: `logs/alerts-YYYY-MM.jsonl` carries an `identity` field per delivered message, and
  `.bot_state.json` holds `pending` + `applied`. Reconstruct from those, do not replace.
- `outbox` — reset to `[]` rather than restored. A stale entry that was already delivered re-sends;
  a genuinely queued row is re-derived by the next parse anyway, since it is absent from `seen`. The
  only real loss is a queued row that has *also* left the upstream table, which is the narrower risk.
- `last_sha` — clear it, so the next run refetches instead of short-circuiting on an unchanged head.
- `seen_legacy_urls` — keep the good commit's value; it is static (see "Retire `seen_legacy_urls`" below).

A small `scripts/` helper that performs that merge, plus a worked example in the README runbook,
is the deliverable. The shape check is the cheaper half and can land first.

Keep both narrow. Over-validating state is how you turn a recoverable blip into a hard outage.

### 5. Dependency and supply-chain maintenance

- Actions are pinned to floating major tags (`actions/checkout@v4`, `actions/setup-python@v5`) in
  both workflows. Pin to full commit SHAs — this workflow holds `contents: write` and Telegram and
  Google credentials, so a compromised tag is a real exposure.
- `google-auth==2.35.0` is pinned exactly (good); `pytest`/`pyyaml` in CI are unpinned (fine, they
  touch nothing).
- Enable Dependabot for `github-actions` so the pins above get PRs rather than rotting.

### 6. Expand source coverage

The biggest product upside and the most work. Adding a board means a `WATCHERS` entry, possibly a
parser variant, and `PARSING_REFERENCE.md` + `README.md` updates in the same commit (`AGENTS.md`
requires this).

The machinery is ready for it: silent bootstrap means adding a source cannot flood the chat, and
`parse_markdown_rows` is column-configurable. Follow the recipe in `TESTING.md` §"Changing a
watcher or a parser" — add the parser test, confirm it fails first, then dry-run and check the row
count for the new source before merging.

### 7. Retire `seen_legacy_urls`

Speedyapply and Zapply carry a static set of bare apply URLs inherited from the pre-2026-07-30
cumulative-URL scheme (22 / 17 / 140 / 156 entries). They match on URL alone, ignoring `term`, so a
requisition relisted for a new season under the same URL will not alert.

**This does not decay, and waiting is not a plan.** The set is static: every run reads
`seen_legacy_urls` and writes it back unchanged (`watch-files.yml:567`, `:752`), and `select_new`
rejects any row whose apply URL is in it regardless of term or occurrence
(`watcher/core.py`). So the counts never fall, they never signal that the field has stopped
mattering, and the suppression of a future-season requisition reusing one of those URLs lasts
indefinitely. Retirement needs an explicit criterion.

**Make it observable, then delete on the signal.** Add a `legacy_hits` counter to the per-run log:
how many parsed rows were suppressed by `seen_legacy_urls` rather than by `seen` this run. It is a
few lines in the loop and it converts "when the counts stop mattering" into something you can read
off `logs/runs-*.jsonl`. Drop the field for a source once its `legacy_hits` has been **0 for 30
consecutive days** — at that point no live row depends on it and removing it changes nothing.
Sources can retire independently; the counts differ widely (22 / 17 / 140 / 156).

**Backstop: 2027-09-01.** These URLs were captured on 2026-07-30 from boards listing Summer 2027
roles. Any of them still live after that season closes is anomalous, so delete the field
unconditionally at that date even if the counter was never added. Waiting longer only extends the
window in which a reused URL is silently suppressed.

Removal touches `migrate_state` and `select_new` in `watcher/core.py`, the state write and read in
`watch-files.yml`, and the shape assertions in `tests/test_core.py`.

### 8. Monitoring dashboard

Now feasible: `logs/runs-YYYY-MM.jsonl` and `logs/alerts-YYYY-MM.jsonl` are append-only and already
carry everything a dashboard would show.

Be honest about the marginal value first. The per-run job summary table already answers "did every
source parse, and how many alerts went out", and it is one click from the Actions tab. A dashboard
earns its keep for *trends* — alert volume per source over weeks, outbox depth over time, which
parser is drifting — not for point-in-time status. Scope it to that or skip it.

Cheapest useful version: a script that renders the JSONL to a static HTML page, run on demand. Do
not add a job that commits generated output — this repo already commits state most minutes.

---

## Considered and not recommended

**Package the watcher as a reusable library.** The extraction of `watcher/core.py` already got the
real benefit: pure logic that unit tests can reach. Going further — a generic
"monitor-a-page-and-notify" abstraction — means designing for hypothetical second users of a
single-user tool, and every abstraction seam added for them is a seam the parsers have to be bent
through. The concrete costs (more indirection between a board's HTML and an alert, a public API to
keep stable) land on the one property that matters here: not missing a job. Revisit only if a real
second consumer appears.

---

## Already shipped

Listed so they are not re-proposed. Merged 2026-07-30 unless noted.

| Idea | Where it landed |
|---|---|
| Activate the external dead-man switch | Configuration, 2026-07-30. healthchecks.io check at period 5m / grace 55m, email and Telegram attached, `HEALTHCHECK_PING_URL` repository secret set. Verified three ways: a real run pinged (`OK` in the step log against a masked, non-empty secret), a dry run's ping step was skipped, and a throwaway 1-minute check produced a notification on both channels. Settings and triage in `README.md` §"The external dead-man switch, as configured". |
| Fold the healthcheck runbook into `README.md` | This PR. The configured period/grace and their rationale, the two-channel and alert-once semantics, the three verification steps, and a triage table keyed on what the Actions tab shows. |
| Automated CI testing | `.github/workflows/tests.yml`, 115 tests across `tests/` — unit, end-to-end against the real workflow heredoc, and structural YAML checks. Path-filtered so state commits do not trigger runs. |
| Improved logging | `logs/runs-*.jsonl` and `logs/alerts-*.jsonl` (append-only), per-run job summary table, `skip_reason` on every source-run, per-alert sequence numbers, and a reconciliation invariant (`queued_before + identities_new == sent_ok + sent_failed`) that makes a missed job detectable. |
| Health checks and stale-run alerts | `health_alert()` with per-kind rate limiting, `⚠️ zero-rows` / `shrink` / `fetch-failed` / `silence` notices, hourly heartbeat, 2-hour silence alarm, and a `healthy` step output gating the external ping. The external switch is now configured too — see the first row of this table. |
| Improved failure recovery | Durable per-source `outbox`, commit-after-deliver (an identity enters `seen` only once Telegram confirms), 429/5xx retry honouring `retry_after`, backoff bounded by the run deadline, and 5 push retries. |
| Safer testing modes | The `dry_run` dispatch input: parses every enabled source, prints what it *would* send, sends nothing, writes nothing, commits nothing, and skips `process_applies`. Deliberately ignores the unchanged-SHA short-circuit so a rehearsal cannot pass by parsing nothing. |
| State validation (partial) | Empty-parse guard, shrink alarm, monotonic append-only `seen`, idempotent `migrate_state`. The remaining gap is shape validation and a documented restore path — open item #3. |
