# Future Improvements

Ranked by usefulness divided by effort — do them roughly top-down.

Before adding to this list, check "Already shipped" at the bottom. Half of the obvious ideas for
this project landed in #1 and #2, and a roadmap that re-proposes finished work is worse than no
roadmap.

---

## Open

### 1. Activate the external dead-man switch

**Config only — no code.** The `Ping healthcheck` step is merged and inert until the
`HEALTHCHECK_PING_URL` repository secret exists.

Every health signal the watcher has is *in-band*: the `⚠️` alerts, the 2-hour silence check and the
heartbeat all require a run to happen. If dispatch stops entirely — cron-job.org's PAT expires, the
account lapses — nothing runs, so nothing reports, and the failure is indistinguishable from a
quiet job market. There is no `schedule:` trigger by design, so this is the single blind spot left.

Set up a healthchecks.io check (period 5m, grace 55m), attach email **and** Telegram, add the
secret. Verify three ways: a real run's ping step is green, a dry run's is skipped, and a throwaway
1-minute check actually produces a notification.

Note the ping is withheld when *any* source is unreadable, so a broken parser trips this too — and
that is the likelier trigger. One look at the Actions tab distinguishes them: recent runs present →
a source broke; nothing since the last ping → dispatch stopped.

### 2. Bound `.bot_state.json` growth

`pending` only ever grows. Entries leave it when you tap `✅ Applied` (`watch-files.yml:1136`) and
in no other way. Currently **674 pending against 33 applied, 300 KB**, rewritten and committed on
most runs — roughly every minute.

Nothing is broken today (the `.git` pack is ~2 MB), but it accretes ~120 entries/day forever, and
every one of those is a full-file rewrite in a new commit. Left alone this is a slow repo-bloat
problem that eventually costs checkout and push time on every run.

Suggested fix: expire `pending` entries older than N days (60?) on write, capped like `seen` is.
The only cost of expiring one is that tapping a very old `✅ Applied` button stops working —
`process_applies` resolves a tap by looking up its hash, so an expired entry means the tap is
ignored. Log expiries so that is diagnosable rather than mysterious.

**Add a test first.** This touches the callback path, which `test_job_hash_matches_the_hashes_in_bot_state`
guards for a reason: every button already in the chat carries a hash.

### 3. Fold the healthcheck runbook into `README.md`

Once #1 is done and the period/grace are settled, move the setup steps and the two-causes triage
into the README's outage runbook. The README currently documents the secret's existence but not how
to interpret an alert from it. Small, and it makes the standalone handoff disposable.

### 4. Re-enable the off-season watchers when their season opens

`SimplifyJobs/Summer2026-Internships` and `vanshb03/Summer2027-Internships` are
`"enabled": False` in `WATCHERS`. This is a calendar item, not an engineering one, but forgetting it
means two sources produce zero alerts and *nothing reports that* — a disabled watcher is
indistinguishable from a quiet one.

Their state is intact (539 and 180 identities, last parsed at their pause SHAs), so flipping the
flag resumes without re-alerting. Worth a calendar reminder rather than a code change.

### 5. Validate state shape on load, and document recovery

`state = json.loads(STATE_FILE.read_text())` at `watch-files.yml:490` has no guard. A truncated or
malformed commit raises, which **fails safe** — the run dies before writing anything, no alerts go
out, no ping fires, so you find out. That is the right failure mode and worth keeping.

What is missing is the other half: a check that the *shape* is sane (each entry has the five
expected keys, `seen` is a list of strings, `outbox` entries are 3-element), and a documented
restore path. Today recovery is "find a good commit in the history of `.watcher_state.json` and
revert it", which works but is not written down anywhere.

Keep this narrow. Over-validating state is how you turn a recoverable blip into a hard outage.

### 6. Dependency and supply-chain maintenance

- Actions are pinned to floating major tags (`actions/checkout@v4`, `actions/setup-python@v5`) in
  both workflows. Pin to full commit SHAs — this workflow holds `contents: write` and Telegram and
  Google credentials, so a compromised tag is a real exposure.
- `google-auth==2.35.0` is pinned exactly (good); `pytest`/`pyyaml` in CI are unpinned (fine, they
  touch nothing).
- Enable Dependabot for `github-actions` so the pins above get PRs rather than rotting.

### 7. Expand source coverage

The biggest product upside and the most work. Adding a board means a `WATCHERS` entry, possibly a
parser variant, and `PARSING_REFERENCE.md` + `README.md` updates in the same commit (`AGENTS.md`
requires this).

The machinery is ready for it: silent bootstrap means adding a source cannot flood the chat, and
`parse_markdown_rows` is column-configurable. Follow the recipe in `TESTING.md` §"Changing a
watcher or a parser" — add the parser test, confirm it fails first, then dry-run and check the row
count for the new source before merging.

### 8. Retire `seen_legacy_urls`

Speedyapply and Zapply carry a static set of bare apply URLs inherited from the pre-2026-07-30
cumulative-URL scheme (22 / 17 / 140 / 156 entries). They match on URL alone, ignoring `term`, so a
requisition relisted for a new season under the same URL will not alert.

This decays on its own — as those rows churn out, their successors are matched on the full identity
— so it needs no action, only tracking, so it does not become unexplained state. Delete the field
and its handling once the counts stop mattering. `migrate_state` and `select_new` both reference
it.

### 9. Monitoring dashboard

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

Listed so they are not re-proposed. All in #1 and #2, merged 2026-07-30.

| Idea | Where it landed |
|---|---|
| Automated CI testing | `.github/workflows/tests.yml`, 115 tests across `tests/` — unit, end-to-end against the real workflow heredoc, and structural YAML checks. Path-filtered so state commits do not trigger runs. |
| Improved logging | `logs/runs-*.jsonl` and `logs/alerts-*.jsonl` (append-only), per-run job summary table, `skip_reason` on every source-run, per-alert sequence numbers, and a reconciliation invariant (`queued_before + identities_new == sent_ok + sent_failed`) that makes a missed job detectable. |
| Health checks and stale-run alerts | `health_alert()` with per-kind rate limiting, `⚠️ zero-rows` / `shrink` / `fetch-failed` / `silence` notices, hourly heartbeat, 2-hour silence alarm, and a `healthy` step output gating the external ping. Only the external switch itself is unconfigured — that is open item #1. |
| Improved failure recovery | Durable per-source `outbox`, commit-after-deliver (an identity enters `seen` only once Telegram confirms), 429/5xx retry honouring `retry_after`, backoff bounded by the run deadline, and 5 push retries. |
| Safer testing modes | The `dry_run` dispatch input: parses every enabled source, prints what it *would* send, sends nothing, writes nothing, commits nothing, and skips `process_applies`. Deliberately ignores the unchanged-SHA short-circuit so a rehearsal cannot pass by parsing nothing. |
| State validation (partial) | Empty-parse guard, shrink alarm, monotonic append-only `seen`, idempotent `migrate_state`. The remaining gap is shape validation and a documented restore path — open item #5. |
