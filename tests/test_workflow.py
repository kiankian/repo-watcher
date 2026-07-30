"""End-to-end tests of the watcher loop, running the real workflow block (see conftest.py).

Two guarantees are under test:
  * nothing is recorded as seen unless Telegram confirmed it (no missed alert), and
  * `seen` only grows, so a shrinking parse cannot resurrect old listings (no duplicate alert).

The state fixture is deliberately still in the legacy {last_sha, rows} shape, so every run here
also exercises the migration.
"""
from conftest import load_fixture

NEW_A = ["TestCorp Alpha", "SWE Intern", "Testville, TS", "Summer 2026",
         "https://example.com/apply/alpha"]
NEW_B = ["TestCorp Beta", "SWE Intern", "Testville, TS", "Summer 2026",
         "https://example.com/apply/beta"]
NEW_C = ["TestCorp Gamma", "SWE Intern", "Testville, TS", "Summer 2026",
         "https://example.com/apply/gamma"]


# --- delivery guarantee -----------------------------------------------------------------

def test_clean_run_alerts_and_records_everything(run_watcher, fixture_rows, baseline):
    r = run_watcher(fixture_rows + [NEW_A, NEW_B, NEW_C])

    assert len(r.sent) == 3
    assert len(r.seen) == len(baseline.seen) + 3
    assert r.last_sha.startswith("deadbeef"), "sha advances on a fully delivered run"


def test_unchanged_upstream_alerts_nothing(run_watcher, fixture_rows, baseline):
    r = run_watcher(fixture_rows)

    assert r.sent == []
    assert len(r.seen) == len(baseline.seen)


def test_transient_rate_limit_is_retried_not_dropped(run_watcher, fixture_rows, baseline):
    """HTTP 429 is flood control, not a client error: it must be retried, honouring
    retry_after. Before this fix urlopen_with_retry re-raised every sub-500 status."""
    r = run_watcher(fixture_rows + [NEW_A, NEW_B, NEW_C], fail_once_at=1)

    assert len(r.sent) == 4, "the 429'd send is attempted twice"
    assert "Rate limited (429)" in r.log
    assert len(r.seen) == len(baseline.seen) + 3, "nothing lost to a transient 429"
    assert r.last_sha.startswith("deadbeef")


def test_undelivered_listing_is_withheld_from_state(run_watcher, fixture_rows, baseline):
    """The core guarantee: a listing whose send never succeeded must not be recorded, or it
    would never be reconsidered and the alert would be lost for good."""
    r = run_watcher(fixture_rows + [NEW_A, NEW_B, NEW_C], fail_for="TestCorp Beta")

    assert r.sent_matching("TestCorp Beta"), "Beta was attempted"
    assert len(r.seen) == len(baseline.seen) + 2, "only the two delivered listings are recorded"
    assert not [i for i in r.seen if "beta" in i]
    assert [row[0] for row, _ident, _occ in r.outbox] == ["TestCorp Beta"], (
        "the undelivered job is queued durably, not left to be re-derived from a later parse"
    )


def test_withheld_listing_is_retried_on_the_next_run(run_watcher, fixture_rows, baseline):
    """Chained from the failed run's own state -- the assertion is meaningless starting from a
    fresh fixture, since the listing would then look new for an unrelated reason."""
    upstream = fixture_rows + [NEW_A, NEW_B, NEW_C]
    failed = run_watcher(upstream, fail_for="TestCorp Beta")

    retry = run_watcher(upstream, start_files=failed.files)

    assert retry.sent_matching("TestCorp Beta"), "the withheld listing is retried"
    assert len(retry.sent) == 1, "already-delivered listings must not re-alert"
    assert len(retry.seen) == len(baseline.seen) + 3
    assert retry.last_sha.startswith("deadbeef")


def test_burst_is_capped_and_the_remainder_deferred(run_watcher, fixture_rows, baseline):
    """Telegram allows ~1 msg/sec to a chat, so a large batch is split across runs. The deferred
    rows must be withheld by the same mechanism as a failed send."""
    extra = [
        [f"BurstCorp {i:03d}", "SWE Intern", "Testville, TS", "Summer 2026",
         f"https://example.com/apply/burst{i}"]
        for i in range(40)
    ]
    first = run_watcher(fixture_rows + extra)

    assert len(first.sent) == 25, "BURST_CAP sends this run"
    assert len(first.outbox) == 15, "the remainder is queued, not dropped"

    second = run_watcher(fixture_rows + extra, start_files=first.files)

    assert len(second.sent) == 15, "the deferred remainder goes out next run"
    assert len(second.seen) == len(baseline.seen) + 40
    assert second.last_sha.startswith("deadbeef")
    delivered = first.sent + second.sent
    for i in range(40):
        assert len([t for t in delivered if f"BurstCorp {i:03d}" in t]) == 1, (
            f"BurstCorp {i:03d} delivered exactly once across the two runs"
        )


def test_zero_extracted_rows_never_advances_state(run_watcher, fixture_rows, baseline):
    """A broken section marker yields an empty parse. That must not wipe `seen`, or the whole
    board would re-alert once the marker came back."""
    r = run_watcher([])

    assert r.sent == []
    assert len(r.seen) == len(baseline.seen), "seen set untouched"
    assert not r.last_sha.startswith("deadbeef")
    assert "extracted 0 rows" in r.log
    assert r.health_matching("parsed 0 rows"), (
        "an indefinitely broken parse looks exactly like 'no new jobs', so it must be reported"
    )


# --- no-duplicate guarantee -------------------------------------------------------------

def test_migration_alerts_nothing_on_the_first_run(run_watcher, fixture_rows):
    """Cutover safety: the legacy fixture is seeded from its stored rows and from bot_state, so
    switching to identities must not replay the board."""
    r = run_watcher(fixture_rows)

    assert r.sent == []
    assert "Migrated" in r.log
    entry = r.state["SimplifyJobs/Summer2026-Internships#summer"]
    assert set(entry) == {
        "last_sha", "seen", "seen_legacy_urls", "last_row_count", "outbox",
    }
    assert entry["last_row_count"] == len(fixture_rows)


def test_a_collapsing_parse_does_not_resurrect_old_listings(run_watcher):
    """The recorded 2026-07-30 01:37 UTC event: 84 rows -> 28, only 5 in common. Under the old
    wholesale snapshot replacement the 79 vanished rows were forgotten and re-alerted on
    return. Here the collapse must alert only the 23 genuinely-new rows, and the recovery must
    alert nothing at all."""
    before = load_fixture("collapse_84.json")
    after = load_fixture("collapse_28.json")

    # Start with no state for this source at all, so the 84 rows are seeded by the silent
    # bootstrap rather than diffed against the unrelated trimmed fixture.
    seeded = run_watcher(before, drop_target_state=True)
    assert seeded.sent == [], "bootstrap stays silent"
    assert len(seeded.seen) == 84

    collapsed = run_watcher(after, start_files=seeded.files)
    assert len(collapsed.sent) == 23, "only the genuinely-new listings alert"
    assert len(collapsed.seen) == 84 + 23, "seen grew; nothing was dropped"

    recovered = run_watcher(before, start_files=collapsed.files)
    assert recovered.sent == [], "the 79 returning rows must not re-alert"
    assert len(recovered.seen) == 84 + 23, "and nothing new is recorded either"


def test_openings_differing_only_by_url_all_alert(run_watcher, fixture_rows):
    """Copart posts several Dallas SWE-intern reqs identical but for the Workday id. row_key
    merged them, so all but one were silently dropped; the identity keeps them apart."""
    reqs = [
        ["Copart", "Software Engineer Intern", "Dallas, TX", "Summer 2026",
         f"https://copart.test/job/JR{n}"]
        for n in (101510, 109672, 109393, 109441)
    ]
    r = run_watcher(fixture_rows + reqs)

    assert len(r.sent) == 4, "every distinct requisition alerts"
    for n in (101510, 109672, 109393, 109441):
        assert r.sent_matching(f"JR{n}"), f"JR{n} alerted"


def test_identical_rows_without_a_url_are_kept_apart(run_watcher, fixture_rows, baseline):
    """Kudu Dynamics lists the same URL-less role three times. Without the occurrence index
    they collapse to one identity and two openings are lost."""
    row = ["Kudu Dynamics", "Software Engineer Intern", "Chantilly, VA", "May 22", ""]
    r = run_watcher(fixture_rows + [row, row, row])

    assert len(r.sent) == 3
    assert len(r.seen) == len(baseline.seen) + 3


def test_url_less_rows_flapping_in_count_do_not_re_alert(run_watcher, fixture_rows):
    """3 -> 2 -> 3 copies of an identical URL-less row. The occurrence identities #1..#3 are
    already in `seen`, so the recovery adds nothing."""
    row = ["Kudu Dynamics", "Software Engineer Intern", "Chantilly, VA", "May 22", ""]
    first = run_watcher(fixture_rows + [row, row, row])
    assert len(first.sent) == 3

    fewer = run_watcher(fixture_rows + [row, row], start_files=first.files)
    assert fewer.sent == []

    again = run_watcher(fixture_rows + [row, row, row], start_files=fewer.files)
    assert again.sent == [], "the third copy returning is not a new opening"


def test_migration_is_idempotent(run_watcher, fixture_rows):
    """Running twice over already-migrated state must not reshape it or alert anything."""
    first = run_watcher(fixture_rows)
    second = run_watcher(fixture_rows, start_files=first.files)

    assert second.sent == []
    assert second.seen == first.seen
    assert "Migrated" not in second.log, "already-migrated state is left alone"


# --- observability ----------------------------------------------------------------------

def test_each_alert_carries_a_gap_free_sequence_number(run_watcher, fixture_rows):
    """The sequence number is what lets a missed alert be spotted from the phone alone, so it
    must not skip: it is committed only after Telegram confirms the send."""
    r = run_watcher(fixture_rows + [NEW_A, NEW_B, NEW_C])

    seqs = [int(t.split("#", 1)[1].split("\n", 1)[0]) for t in r.sent]
    assert seqs == [1, 2, 3]
    assert r.bot_state["seq"] == 3


def test_a_failed_send_does_not_consume_a_sequence_number(run_watcher, fixture_rows):
    """A gap would otherwise be indistinguishable from a lost alert, making the whole signal
    useless. The number is reused by the retry."""
    upstream = fixture_rows + [NEW_A, NEW_B, NEW_C]
    failed = run_watcher(upstream, fail_for="TestCorp Beta")

    delivered = [int(t.split("#", 1)[1].split("\n", 1)[0])
                 for t in failed.sent if "TestCorp Beta" not in t]
    assert delivered == [1, 2], "Alpha and Gamma take 1 and 2; Beta's attempt consumed nothing"
    assert failed.bot_state["seq"] == 2

    retry = run_watcher(upstream, start_files=failed.files)
    assert [int(t.split("#", 1)[1].split("\n", 1)[0]) for t in retry.sent] == [3]


def test_every_delivered_alert_is_appended_to_the_alert_log(run_watcher, fixture_rows):
    """bot_state["pending"] is keyed by job hash, so a re-send overwrites its own record there.
    This log is append-only, which is what makes duplicates and misses countable."""
    r = run_watcher(fixture_rows + [NEW_A, NEW_B, NEW_C])

    records = r.log_records("alerts")
    assert len(records) == 3
    assert [rec["seq"] for rec in records] == [1, 2, 3]
    assert {rec["company"] for rec in records} == {
        "TestCorp Alpha", "TestCorp Beta", "TestCorp Gamma"
    }
    for rec in records:
        assert set(rec) >= {"ts", "seq", "message_id", "watcher", "identity", "job_hash",
                            "company", "role", "location", "term", "apply_url"}


def test_an_undelivered_listing_is_not_written_to_the_alert_log(run_watcher, fixture_rows):
    r = run_watcher(fixture_rows + [NEW_A, NEW_B, NEW_C], fail_for="TestCorp Beta")

    companies = [rec["company"] for rec in r.log_records("alerts")]
    assert "TestCorp Beta" not in companies, "the log records deliveries, not attempts"
    assert len(companies) == 2


def test_a_run_that_alerts_is_written_to_the_run_log(run_watcher, fixture_rows):
    """Turns the 84-to-28 collapse into a visible time series instead of something only findable
    by diffing state commits."""
    r = run_watcher(fixture_rows + [NEW_A])

    runs = [rec for rec in r.log_records("runs")
            if rec.get("state_key") == "SimplifyJobs/Summer2026-Internships#summer"]
    assert len(runs) == 1
    assert runs[0]["rows_extracted"] == len(fixture_rows) + 1
    assert runs[0]["identities_new"] == 1
    assert runs[0]["sent_ok"] == 1
    assert runs[0]["sent_failed"] == 0


def test_a_shrinking_parse_is_reported_even_though_dedup_survives_it(run_watcher):
    """The collapse is harmless to dedup now, but it still signals a truncating parse."""
    before = load_fixture("collapse_84.json")
    after = load_fixture("collapse_28.json")

    seeded = run_watcher(before, drop_target_state=True)
    collapsed = run_watcher(after, start_files=seeded.files)

    assert collapsed.health_matching("down from 84")
    assert len(collapsed.sent) == 23, "and the genuinely-new listings still go out"


def test_dry_run_sends_nothing_and_writes_nothing(run_watcher, fixture_rows):
    """Lets the cutover be rehearsed against live upstream data before any message can go out."""
    before = run_watcher(fixture_rows)
    r = run_watcher(fixture_rows + [NEW_A, NEW_B, NEW_C],
                    start_files=before.files, dry_run=True)

    assert r.sent == [] and r.health == []
    assert r.logs == {}
    assert r.files[".watcher_state.json"] == before.files[".watcher_state.json"]
    assert r.files[".bot_state.json"] == before.files[".bot_state.json"]
    assert "[dry-run] would send #1" in r.log
    assert "state files not written" in r.log


def test_the_step_summary_tabulates_every_watcher(run_watcher, fixture_rows):
    """Actions logs expire after 90 days; this makes one run diagnosable at a glance while it
    still exists, and is where an anomaly shows up if the Telegram notice was rate-limited."""
    before = run_watcher(fixture_rows)
    r = run_watcher(fixture_rows + [NEW_A], start_files=before.files, capture_summary=True)

    assert "| Watcher | rows | prev | new | sent | failed | note |" in r.summary
    assert "| Simplify Summer Repo |" in r.summary
    assert "Watcher run" in r.summary


def test_the_step_summary_lists_anomalies(run_watcher):
    r = run_watcher([], capture_summary=True)

    assert "**Anomalies**" in r.summary
    assert "zero-rows" in r.summary


# --- review fixes -----------------------------------------------------------------------

KUDU = ["Kudu Dynamics", "Software Engineer Intern", "Chantilly, VA", "May 22", ""]


def test_identical_rows_get_separate_applied_button_records(run_watcher, fixture_rows):
    """The occurrence index made all three copies alert, but they shared one job_hash, so each
    send overwrote bot_state["pending"][h]. Tapping an earlier alert then edited and logged the
    last message, and once that entry was deleted the siblings answered "Already logged" for
    good. Three delivered alerts, three records, three message ids."""
    r = run_watcher(fixture_rows + [KUDU, KUDU, KUDU])

    assert len(r.sent) == 3
    kudu = [job for job in r.bot_state["pending"].values() if job["company"] == "Kudu Dynamics"]
    assert len(kudu) == 3, "one pending record per delivered alert"
    assert len({job["message_id"] for job in kudu}) == 3, "each points at its own message"
    assert len({job["identity"] for job in kudu}) == 3
    assert not r.health_matching("already pending"), "the collision alarm must not fire"


def test_the_pending_record_names_its_opening(run_watcher, fixture_rows):
    r = run_watcher(fixture_rows + [NEW_A])

    job = next(j for j in r.bot_state["pending"].values() if j["company"] == "TestCorp Alpha")
    assert job["identity"] == (
        "https://example.com/apply/alpha|TestCorp Alpha|SWE Intern|Testville, TS|Summer 2026#1"
    )


def test_bootstrap_does_not_record_current_urls_as_legacy(run_watcher, fixture_rows):
    """A legacy URL suppresses a row whatever its term, so seeding them at bootstrap would stop a
    requisition relisted for a new season from ever alerting. The seeded identities already
    silence the snapshot; only state that recorded no term belongs in the legacy set."""
    r = run_watcher(fixture_rows, drop_target_state=True)

    entry = r.state["SimplifyJobs/Summer2026-Internships#summer"]
    assert entry["seen_legacy_urls"] == []
    assert len(entry["seen"]) == len(fixture_rows), "identities do the silencing"


def test_a_second_copy_of_a_bootstrapped_url_still_alerts(run_watcher, fixture_rows):
    """Consequence of the above, driven through the real loop. Bootstrap sees one copy of a URL;
    upstream then lists it twice. The #2 occurrence is a distinct, unseen identity and must
    alert -- with the URL in seen_legacy_urls it was rejected on the URL check instead."""
    dupe = ["Acme", "SWE Intern", "NYC", "Summer 2026", "https://acme.test/job/1"]
    seeded = run_watcher(fixture_rows + [dupe], drop_target_state=True)
    assert seeded.sent == []

    r = run_watcher(fixture_rows + [dupe, dupe], start_files=seeded.files)

    assert len(r.sent) == 1, "the second occurrence is a new opening"
    assert r.sent_matching("https://acme.test/job/1")


# --- durable outbox ---------------------------------------------------------------------

def _burst(n, prefix="Queued"):
    return [
        [f"{prefix} {i:03d}", "SWE Intern", "Testville, TS", "Summer 2026",
         f"https://example.com/apply/{prefix.lower()}{i}"]
        for i in range(n)
    ]


def test_a_deferred_job_survives_leaving_the_upstream_table(run_watcher, fixture_rows):
    """The assertion the previous design could not make. Withholding a row from `seen` only
    guaranteed it would be *reconsidered*; the retry re-derived it from a fresh parse, so a row
    that left the table in the meantime was gone. Zapply's table re-sorts and is capped at ~100
    rows, so this is not hypothetical."""
    extra = _burst(40)
    first = run_watcher(fixture_rows + extra)
    assert len(first.sent) == 25 and len(first.outbox) == 15

    # Upstream now drops every one of the deferred rows before the retry.
    second = run_watcher(fixture_rows, start_files=first.files)

    assert len(second.sent) == 15, "queued jobs are delivered from the outbox, not re-parsed"
    assert second.outbox == []
    delivered = first.sent + second.sent
    for i in range(40):
        assert len([t for t in delivered if f"Queued {i:03d}" in t]) == 1


def test_a_failed_send_survives_leaving_the_upstream_table(run_watcher, fixture_rows):
    """Same guarantee for a genuine delivery failure rather than the burst cap."""
    failed = run_watcher(fixture_rows + [NEW_A, NEW_B, NEW_C], fail_for="TestCorp Beta")
    assert [row[0] for row, _i, _o in failed.outbox] == ["TestCorp Beta"]

    recovered = run_watcher(fixture_rows, start_files=failed.files)

    assert recovered.sent_matching("TestCorp Beta"), "delivered from the queue"
    assert len(recovered.sent) == 1
    assert recovered.outbox == []


def test_the_outbox_drains_without_an_upstream_change(run_watcher, fixture_rows):
    """A queued job must not wait on an unrelated upstream commit. The loop short-circuits on an
    unchanged sha, so that check has to yield while the outbox has work."""
    extra = _burst(30)
    first = run_watcher(fixture_rows + extra)
    assert len(first.outbox) == 5

    # Re-run against the *same* head sha the first run recorded.
    second = run_watcher(fixture_rows + extra, start_files=first.files,
                         reuse_sha=first.last_sha)

    assert "sha unchanged, draining 5 queued job(s)" in second.log
    assert len(second.sent) == 5
    assert second.outbox == []


def test_queued_jobs_are_not_double_sent_when_still_listed(run_watcher, fixture_rows):
    """The outbox and a fresh parse overlap. Sending from both would duplicate every deferred
    job on the next run."""
    extra = _burst(30)
    first = run_watcher(fixture_rows + extra)

    second = run_watcher(fixture_rows + extra, start_files=first.files)

    assert len(second.sent) == 5, "the 5 queued jobs, not 5 queued plus 5 re-parsed"
    delivered = first.sent + second.sent
    for i in range(30):
        assert len([t for t in delivered if f"Queued {i:03d}" in t]) == 1


def test_queued_jobs_drain_before_newly_seen_rows(run_watcher, fixture_rows):
    """Otherwise a steady stream of new listings can starve the queue indefinitely."""
    first = run_watcher(fixture_rows + _burst(30))
    assert len(first.outbox) == 5

    second = run_watcher(fixture_rows + _burst(30) + _burst(30, "Newer"),
                         start_files=first.files)

    assert len(second.sent_matching("Queued ")) == 5, "the whole queue went first"
    assert len(second.sent) == 25, "then the burst cap filled with new rows"


def test_the_run_log_reports_the_queue_depth(run_watcher, fixture_rows):
    """A queue that never drains is the miss signal the sequence numbers cannot provide."""
    r = run_watcher(fixture_rows + _burst(40))

    record = next(rec for rec in r.log_records("runs")
                  if rec.get("state_key") == "SimplifyJobs/Summer2026-Internships#summer")
    assert record["outbox_size"] == 15
    assert record["sent_ok"] == 25


# --- fetch failures are not healthy -----------------------------------------------------

def test_a_fetch_failure_is_alerted_and_not_reported_healthy(run_watcher, fixture_rows):
    """An upstream rename of the watched file stops this source for good. Previously it was
    printed and skipped while the run still pinged the external watchdog as healthy."""
    r = run_watcher(fixture_rows, fetch_status=404)

    assert r.sent == []
    assert r.health_matching("could not fetch"), "the outage has to reach the chat"
    assert r.healthy is False, "the ping must be withheld"
    assert len(r.seen) > 0, "state is left intact for when the file comes back"


def test_a_healthy_run_reports_healthy(run_watcher, fixture_rows):
    r = run_watcher(fixture_rows + [NEW_A])

    assert r.healthy is True


def test_a_zero_row_parse_is_also_not_healthy(run_watcher, fixture_rows):
    """Same class as a fetch failure: the source produces nothing until someone fixes it."""
    r = run_watcher([])

    assert r.healthy is False


def test_a_fetch_failure_keeps_the_queue(run_watcher, fixture_rows):
    """A source that cannot be read must not lose work already queued for it."""
    first = run_watcher(fixture_rows + _burst(40))
    assert len(first.outbox) == 15

    r = run_watcher(fixture_rows, start_files=first.files, fetch_status=404)

    assert len(r.outbox) == 15, "the queue survives an unreadable source"
    assert r.healthy is False
