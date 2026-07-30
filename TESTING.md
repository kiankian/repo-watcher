# Testing repo-watcher

The watcher exists to make one promise: **every genuinely new in-scope listing produces exactly
one alert.** Both halves matter. A missed job is invisible — you cannot notice an alert that never
arrived — and a stream of duplicates trains you to stop reading the ones that are real.

Everything here protects those two properties. Run the suite before merging anything that touches
parsing, deduplication, delivery or state.

## Quick start

```sh
pip install pytest pyyaml
python -m pytest -q
```

No secrets, no network, no writes to the committed state files. The whole suite takes ~2 seconds.

`pyyaml` is not optional: `tests/test_workflow_yaml.py` parses the workflow, and without it
collection aborts before a single test runs.

## How the suite is arranged

| File | Layer | What it covers |
|---|---|---|
| `tests/test_core.py` | unit | Pure logic in `watcher/core.py` — parsers, identities, `job_hash`, migration |
| `tests/test_workflow.py` | end-to-end | The watcher loop itself, run for real with the network stubbed |
| `tests/test_workflow_yaml.py` | structural | Job- and step-level `if:` guards, which only GitHub Actions evaluates |
| `tests/conftest.py` | harness | Machinery the end-to-end tests run on |
| `tests/fixtures/` | data | Frozen state snapshots |

### The harness runs the real workflow

The decision that matters most — *is this listing new, and may I record it as seen?* — lives in
the workflow's main loop, not in `watcher/core.py`. Reimplementing that loop in the tests would
mean testing a copy that can drift from the thing that ships.

So `tests/conftest.py` extracts the `python - <<'PY'` heredoc straight out of
`.github/workflows/watch-files.yml`, execs it against a temporary checkout with
`urllib.request.urlopen` replaced by a stub, and inspects the result. A test therefore drives the
same code CI runs, and refactoring the loop cannot silently stop it being covered.

The `run_watcher` fixture returns a `Result` exposing everything a run did:

| Attribute | Contents |
|---|---|
| `sent` / `health` | Job alerts and `⚠️` notices, kept apart so "no alerts" is not satisfied by an operational notice |
| `seen` / `outbox` | The target source's identity set and queue after the run |
| `state` / `bot_state` / `files` | The written files — pass `files` as `start_files=` to chain runs |
| `logs` / `log_records(prefix)` | The JSONL the run appended |
| `sleeps` / `timeouts` / `fetches` | Durations requested, per-call timeouts, and raw-file fetches |
| `summary` / `step_output` / `healthy` | The Actions job summary and the ping-gating output |

Options: `fail_for=`, `fail_all=`, `fail_once_at=`, `retry_after=`, `fetch_status=`, `dry_run=`,
`reuse_sha=`, `drop_target_state=`, `monotonic_step=`, `capture_summary=`.

### Fixtures are frozen on purpose

`tests/fixtures/watcher_state.json` and `bot_state.json` are trimmed copies, not the live files —
the runner rewrites those every minute, and tests reading them are not reproducible.

`collapse_84.json` / `collapse_28.json` are the real snapshots either side of the row collapse of
2026-07-30 01:37 UTC, when a source fell from 84 rows to 28 with only 5 in common. That event is
replayed as a regression test.

One test deliberately reads the **live** `.watcher_state.json`
(`test_migrating_the_real_state_files_is_safe`), because it is the only check that runs against
production data. It adapts to whichever shape the file is in.

## What each guarantee rests on

If you change behaviour, one of these should fail. If you change behaviour and none do, the change
is not covered — write the test before writing the fix.

**No missed alert**

| Property | Test |
|---|---|
| A failed send is never recorded as seen | `test_undelivered_listing_is_withheld_from_state` |
| …and is retried on a later run | `test_withheld_listing_is_retried_on_the_next_run` |
| …even if it has left the upstream table | `test_a_deferred_job_survives_leaving_the_upstream_table` |
| …even if the source is unreadable | `test_a_fetch_failure_still_drains_the_queue` |
| …and without waiting for an upstream commit | `test_the_outbox_drains_without_an_upstream_change` |
| Rate limits are retried, not dropped | `test_transient_rate_limit_is_retried_not_dropped` |
| Same-titled openings stay distinct | `test_openings_differing_only_by_url_all_alert` |
| Identical URL-less rows stay distinct | `test_identical_rows_without_a_url_are_kept_apart` |
| A replacement behind a shared link is not swallowed | `test_a_replacement_sharing_a_generic_url_is_not_suppressed` |
| Identities are total and injective | `test_identity_is_total_and_injective_over_the_recorded_snapshots` |

**No duplicate alert**

| Property | Test |
|---|---|
| A shrinking parse does not resurrect old listings | `test_a_collapsing_parse_does_not_resurrect_old_listings` |
| A zero-row parse never wipes state | `test_zero_extracted_rows_never_advances_state` |
| Queued rows still listed upstream are not re-sent | `test_queued_jobs_are_not_double_sent_when_still_listed` |
| Migration replays nothing | `test_migration_alerts_nothing_on_the_first_run` |
| Bootstrap is silent | seeding phase of `test_a_collapsing_parse_does_not_resurrect_old_listings` |

**The run cannot harm itself**

| Property | Test |
|---|---|
| Attempts are capped, not just successes | `test_a_total_outage_caps_attempts_not_successes` |
| One health notice per outage, not per job | `test_a_total_outage_alerts_once_not_once_per_job` |
| A long flood wait is not slept through | `test_a_long_flood_wait_is_not_slept_through` |
| …but a short one still is | `test_a_short_flood_wait_is_still_honoured` |
| Every network call is bounded | `test_every_network_call_is_bounded_by_a_timeout` |
| Silence is reported, not swallowed | `test_a_fetch_failure_is_alerted_and_not_reported_healthy` |
| Nothing writes state off the default branch | `test_state_writing_is_confined_to_the_default_branch` |

**Compatibility.** `test_job_hash_matches_the_hashes_in_bot_state` protects users rather than
logic. Every `✅ Applied` button already in the chat carries a hash, and `process_applies`
resolves a tap by looking it up. If that test fails, every outstanding button has been orphaned.
Treat it as a hard constraint, not a test to update.

## Traps specific to this suite

Four tests here have been green for the wrong reason at some point. Each mistake is easy to repeat.

**Stubbing hides what you are asserting.** `time.sleep` is stubbed to a no-op, so an hour-long
wait looks instant and "the run finished" proves nothing about a deadline. Assert on
`result.sleeps` — what the code *asked* for. Same for `result.timeouts`: a stub that returns
immediately makes an unbounded call indistinguishable from a bounded one.

**Failure injection must match the failure you mean.** `fail_for=<label>` looked like a total
outage but did not match the send-failure notice, so the health send succeeded, the rate limiter
engaged for an unrelated reason, and the test passed against the broken code. Use `fail_all=True`
for an outage: real outages take the notices down too.

**Scenario choice can make an invariant hold by accident.** A counter test drained a queue against
an upstream that had *dropped* the queued rows, so an overlap bug never appeared. Parameterize
over both upstreams — dropped and still-listed — rather than picking one.

**Chained runs need distinct SHAs.** The loop short-circuits on an unchanged head, so reusing a
SHA makes the second run a no-op for reasons unrelated to the test. The harness issues a fresh one
per run; pass `reuse_sha=` only when the unchanged-SHA path is what you are testing.

**Prove the test fails without the fix.** Every fix in this repo was verified by checking out the
previous workflow and confirming the new test fails against it:

```sh
cp .github/workflows/watch-files.yml /tmp/fixed.yml
git checkout HEAD~1 -- .github/workflows/watch-files.yml
python -m pytest -q tests/test_workflow.py::test_your_new_test   # must FAIL
cp /tmp/fixed.yml .github/workflows/watch-files.yml
python -m pytest -q                                              # must PASS
```

If it passes both ways it is a guard-rail, not a regression test. Both are worth having — just
know which you wrote.

## Checking against live data

The suite never touches the network. To validate parsers against the boards as they are now, use a
dry run: **Actions → Watch files in external repo → Run workflow → tick `dry_run`**.

It fetches and parses every enabled source, computes identities, prints what it *would* send, then
sends nothing, writes nothing and commits nothing. The callback job is skipped, so the Google
Sheet is untouched. It also ignores the unchanged-SHA short-circuit a normal run uses — otherwise
a rehearsal minutes after a live run would skip everything and report success having parsed
nothing.

Read the summary table: `rows` should be plausible and non-zero per watcher, and an **Anomalies**
section means a parser is broken.

Dispatch it from the **default branch**. Any other ref is forced into dry mode anyway, and the
`dry_run` checkbox does not render for an unmerged branch, because GitHub builds the form from the
default branch.

## Changing a watcher or a parser

1. Add the case to `tests/test_core.py` if it is parsing, `tests/test_workflow.py` if it is a
   decision about alerting. Confirm it fails without your change.
2. `python -m pytest -q`
3. Dry-run against live data and check the row count for the source you touched.
4. If you touched `migrate_state`, the identity format or `job_hash`, rehearse the transition
   against copies of the real state files and confirm it sends nothing. Changing the identity
   format after deployment needs a dual-format check — existing `seen` entries are in the old
   format and would all look new.

For a pull request, include: the test that fails without the change, the suite result, and the
dry-run row counts.

## What is deliberately not tested

- **Live delivery.** No test sends a real Telegram message or writes to the Google Sheet. Confirm
  those by tapping `✅ Applied` on a real alert after deploying.
- **The cron trigger.** `cron-job.org` dispatching the workflow is outside the repo; see the
  runbook in `README.md` if alerts go quiet.
- **Upstream page structure.** Nothing pins the boards' HTML. That is what the `⚠️ zero-rows` and
  shrink alerts are for at runtime, and what the dry run is for before merging.

A green suite says the code behaves; it cannot say a board still looks the way the parser expects,
or that the workflow is being dispatched at all. Those are runtime concerns, covered by the health
alerts and the optional external dead-man switch described in `README.md`.

## CI

`.github/workflows/tests.yml` runs the suite on pushes and pull requests touching `watcher/`,
`tests/`, `pytest.ini` or either workflow. The path filter is deliberate: the watcher pushes a
state commit most minutes, and without it every one would trigger a test run.
