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
    assert not r.last_sha.startswith("deadbeef"), (
        "sha is held back so the next run re-fetches instead of short-circuiting"
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
    assert not first.last_sha.startswith("deadbeef"), "partial run holds the sha back"

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


# --- no-duplicate guarantee -------------------------------------------------------------

def test_migration_alerts_nothing_on_the_first_run(run_watcher, fixture_rows):
    """Cutover safety: the legacy fixture is seeded from its stored rows and from bot_state, so
    switching to identities must not replay the board."""
    r = run_watcher(fixture_rows)

    assert r.sent == []
    assert "Migrated" in r.log
    entry = r.state["SimplifyJobs/Summer2026-Internships#summer"]
    assert set(entry) == {"last_sha", "seen", "seen_legacy_urls", "last_row_count"}
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
