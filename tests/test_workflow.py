"""Delivery-guarantee tests: no listing may be recorded as seen unless it was delivered.

These run the real workflow block (see conftest.py) so the guarantee is tested against the code
that actually ships, not a reimplementation.
"""
NEW_A = ["TestCorp Alpha", "SWE Intern", "Testville, TS", "Summer 2026",
         "https://example.com/apply/alpha"]
NEW_B = ["TestCorp Beta", "SWE Intern", "Testville, TS", "Summer 2026",
         "https://example.com/apply/beta"]
NEW_C = ["TestCorp Gamma", "SWE Intern", "Testville, TS", "Summer 2026",
         "https://example.com/apply/gamma"]


def test_clean_run_alerts_and_records_everything(run_watcher, fixture_rows):
    r = run_watcher(fixture_rows + [NEW_A, NEW_B, NEW_C])

    assert len(r.sent) == 3
    assert len(r.rows) == len(fixture_rows) + 3
    assert r.last_sha.startswith("deadbeef"), "sha advances on a fully delivered run"


def test_unchanged_upstream_alerts_nothing(run_watcher, fixture_rows):
    """A run that finds no new rows must stay silent -- guards against a dedup regression
    turning every run into a re-alert storm."""
    r = run_watcher(fixture_rows)

    assert r.sent == []
    assert len(r.rows) == len(fixture_rows)


def test_transient_rate_limit_is_retried_not_dropped(run_watcher, fixture_rows):
    """HTTP 429 is flood control, not a client error: it must be retried, honouring
    retry_after. Before this fix, urlopen_with_retry re-raised every sub-500 status."""
    r = run_watcher(fixture_rows + [NEW_A, NEW_B, NEW_C], fail_once_at=1)

    assert len(r.sent) == 4, "the 429'd send is attempted twice"
    assert "Rate limited (429)" in r.log
    assert len(r.rows) == len(fixture_rows) + 3, "nothing lost to a transient 429"
    assert r.last_sha.startswith("deadbeef")


def test_undelivered_listing_is_withheld_from_state(run_watcher, fixture_rows):
    """The core guarantee. A listing whose send never succeeded must not be recorded, or it
    would never be reconsidered and the alert would be lost for good."""
    r = run_watcher(fixture_rows + [NEW_A, NEW_B, NEW_C], fail_for="TestCorp Beta")

    assert r.sent_matching("TestCorp Beta"), "Beta was attempted"
    assert len(r.rows) == len(fixture_rows) + 2, "only the two delivered listings are recorded"
    assert not [row for row in r.rows if row[0] == "TestCorp Beta"]
    assert not r.last_sha.startswith("deadbeef"), (
        "sha is held back so the next run re-fetches instead of short-circuiting"
    )


def test_withheld_listing_is_retried_on_the_next_run(run_watcher, fixture_rows):
    """Chained from the failed run's own state -- the assertion is meaningless if it starts
    from a fresh fixture, since then the listing would look new for an unrelated reason."""
    upstream = fixture_rows + [NEW_A, NEW_B, NEW_C]
    failed = run_watcher(upstream, fail_for="TestCorp Beta")

    retry = run_watcher(upstream, start_files=failed.files)

    assert retry.sent_matching("TestCorp Beta"), "the withheld listing is retried"
    assert len(retry.sent) == 1, "already-delivered listings must not re-alert"
    assert len(retry.rows) == len(fixture_rows) + 3
    assert retry.last_sha.startswith("deadbeef")


def test_burst_is_capped_and_the_remainder_deferred(run_watcher, fixture_rows):
    """Telegram allows ~1 msg/sec to a chat. A large batch is split across runs, and the
    deferred rows must be withheld -- the same mechanism as a failed send."""
    extra = [
        [f"BurstCorp {i:03d}", "SWE Intern", "Testville, TS", "Summer 2026",
         f"https://example.com/apply/burst{i}"]
        for i in range(40)
    ]
    first = run_watcher(fixture_rows + extra)

    assert len(first.sent) == 25, "BURST_CAP sends this run"
    assert len(first.rows) == len(fixture_rows) + 25
    assert not first.last_sha.startswith("deadbeef"), "partial run holds the sha back"

    second = run_watcher(fixture_rows + extra, start_files=first.files)

    assert len(second.sent) == 15, "the deferred remainder goes out next run"
    assert len(second.rows) == len(fixture_rows) + 40
    assert second.last_sha.startswith("deadbeef")
    # Every burst listing delivered exactly once across the two runs.
    delivered = first.sent + second.sent
    for i in range(40):
        assert len([t for t in delivered if f"BurstCorp {i:03d}" in t]) == 1


def test_zero_extracted_rows_never_advances_state(run_watcher, fixture_rows):
    """A broken section marker yields an empty parse. That must not wipe the snapshot, or the
    whole board would re-alert once the marker came back."""
    r = run_watcher([])

    assert r.sent == []
    assert len(r.rows) == len(fixture_rows), "snapshot untouched"
    assert not r.last_sha.startswith("deadbeef")
    assert "extracted 0 rows" in r.log
