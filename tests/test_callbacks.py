"""End-to-end tests for the process_applies block of watch-files.yml.

This is the `✅ Applied` path: poll Telegram, look the tapped hash up in `pending`, append a row to
the Sheet, tick the message, advance the offset. None of it had a test before -- block 1 was never
executed by the suite -- so the happy path here is as much the point as the failure handling.

The failure handling exists because a six-minute Telegram stall on 2026-08-03 turned into three
failed and three cancelled runs: `get_me()`, a diagnostic, died before a single tap was read, and
the 3 x 30s retry ladder stretched each run past the one-minute dispatch interval, so the queued
dispatch behind it was evicted. Polling now fails soft -- but only for as long as it is plausibly
a blip, because Telegram discards undelivered updates after 24 hours.
"""
import re
import urllib.error

from conftest import (
    callback_bot_state,
    callback_update,
    http_error,
    pending_job,
    timeout_error,
)

HASH = "208463db3fc2"

# One stall is not enough to fail a poll: POLL_ATTEMPTS is 2, so the retry would succeed and the
# run would never reach the handler under test.
STALL = [timeout_error, timeout_error]


def one_pending(**kw):
    return callback_bot_state(pending={HASH: pending_job(**kw)})


# --- the tap itself -----------------------------------------------------------------------

def test_a_tap_appends_a_sheet_row_and_ticks_the_message(run_callbacks):
    result = run_callbacks(
        updates=[callback_update(7, HASH)],
        start_state=one_pending(company="Walleye Capital", role="Data Science Intern"),
    )

    assert result.code == 0
    company, role, applied_date, email, resource, status = result.appended[0]
    assert (company, role) == ("Walleye Capital", "Data Science Intern")
    assert re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", applied_date), applied_date
    assert (email, resource, status) == ("", "Telegram Bot", "Applied")
    assert len(result.appended) == 1, "one tap, one row"


def test_a_tap_moves_the_hash_from_pending_to_applied(run_callbacks):
    result = run_callbacks(updates=[callback_update(7, HASH)], start_state=one_pending())

    assert HASH not in result.pending, "a logged tap must not be re-appendable"
    assert result.applied[HASH]["company"] == "Acme"
    assert result.applied[HASH]["applied_at"].endswith("Z")
    assert result.offset == 7, "the offset advances only after the append and the edit"


def test_the_edited_message_keeps_its_sequence_number(run_callbacks):
    """The number is what makes a gap in the chat's numbering visible; the edit must not erase
    the evidence."""
    result = run_callbacks(
        updates=[callback_update(7, HASH)],
        start_state=one_pending(seq=41),
    )

    text = result.edits[0]["text"]
    assert text.startswith("Vansh Summer Repo #41")
    assert "✅ Logged" in text
    assert result.edits[0]["reply_markup"] == {"inline_keyboard": []}
    assert result.answers[0]["text"] == "Logged ✅"


def test_a_stale_hash_is_answered_without_appending_anything(run_callbacks):
    """A tap on a button whose job was already logged, or that predates the current state."""
    result = run_callbacks(
        updates=[callback_update(8, "nosuchhash")],
        start_state=callback_bot_state(),
    )

    assert result.appended == [], "nothing to log means nothing written to the Sheet"
    assert result.answers[0]["text"] == "Already logged"
    assert result.offset == 8, "a stale tap must still be consumed, or it repeats every run"
    assert result.code == 0


def test_a_quiet_run_leaves_the_state_file_byte_identical(run_callbacks):
    """The counter is reset on the no-tap path, which is the overwhelmingly common one, so that
    write happens roughly every minute. It must converge: a file that differed each run would
    be a state commit a minute, on top of the watcher's own."""
    first = run_callbacks(start_state=callback_bot_state())
    second = run_callbacks(start_state=first.bot_state_text)

    assert second.bot_state_text == first.bot_state_text
    assert second.poll_failures == 0


def test_a_failed_append_leaves_the_tap_to_be_retried(run_callbacks):
    """Unchanged by this work, and pinned because it is what the offset ordering buys: the row
    is what may not be lost, so nothing is consumed until the Sheet has it."""
    result = run_callbacks(
        updates=[callback_update(7, HASH)],
        start_state=one_pending(),
        faults={"sheets:append": [http_error(403)]},
    )

    assert result.appended == []
    assert HASH in result.pending, "an unlogged tap must stay pending"
    assert result.offset == 0, "and must not be consumed"
    assert "Sheet update failed" in result.answers[-1]["text"]
    assert result.code == 0


def test_a_failure_after_the_append_still_leaves_the_hash_pending(run_callbacks):
    """The known duplicate-row hazard (FUTURE_IMPROVEMENTS.md item 3), asserted so this change
    can be shown not to have moved it: the row lands, the edit dies, and the retry next run
    appends a second row. Shortening the *poll* budget is safe precisely because this path
    keeps the full one."""
    result = run_callbacks(
        updates=[callback_update(7, HASH)],
        start_state=one_pending(),
        faults={"editMessageText": [timeout_error] * 3},
    )

    assert len(result.appended) == 1, "the row is already in the Sheet"
    assert HASH in result.pending, "so the retry will append it again — known, and unchanged"
    assert result.offset == 0


def test_no_state_file_is_not_an_error(run_callbacks):
    result = run_callbacks(state_file=False)

    assert result.code == 0
    assert "nothing to process" in result.log


# --- a stalled Telegram must not fail the run ---------------------------------------------

def test_a_stalled_poll_ends_the_run_green(run_callbacks):
    result = run_callbacks(updates=[callback_update(7, HASH)], start_state=one_pending(),
                           poll_errors=STALL)

    assert result.code == 0, "a Telegram blip is not a broken watcher"
    assert result.appended == [], "nothing may be mutated when the poll never returned"
    assert result.offset == 0, "the offset must stay put so the tap is re-read next run"
    assert HASH in result.pending
    assert result.poll_failures == 1


def test_consecutive_stalls_accumulate(run_callbacks):
    result = run_callbacks(
        start_state=callback_bot_state(health={"callback_poll_failures": 3}),
        poll_errors=STALL,
    )

    assert result.poll_failures == 4
    assert result.code == 0


def test_a_successful_poll_resets_the_count(run_callbacks):
    result = run_callbacks(start_state=callback_bot_state(health={"callback_poll_failures": 4}))

    assert result.poll_failures == 0, "a recovered poller must not inherit the old count"
    assert "recovered after 4" in result.log
    assert result.code == 0


def test_a_sustained_outage_still_goes_red(run_callbacks):
    """Telegram drops undelivered updates after 24h, so a poller stuck for good silently loses
    every tap. Ten quiet runs is the whole tolerance."""
    result = run_callbacks(
        start_state=callback_bot_state(health={"callback_poll_failures": 9}),
        poll_errors=STALL,
    )

    assert result.code == 1
    assert "10 runs in a row" in result.message
    assert "24h" in result.message


def test_flood_control_is_treated_as_transient(run_callbacks):
    """429 is Telegram rate-limiting the bot, not rejecting it."""
    result = run_callbacks(start_state=callback_bot_state(), poll_errors=[http_error(429)])

    assert result.code == 0
    assert result.poll_failures == 1


def test_a_revoked_bot_token_fails_the_run_immediately(run_callbacks):
    """The counter must never swallow a client error: a 401 will not come back on its own, and
    every tap is being lost while it stands."""
    result = run_callbacks(start_state=callback_bot_state(), poll_errors=[http_error(401)])

    assert result.code == 1
    assert isinstance(result.error, urllib.error.HTTPError) and result.error.code == 401
    assert result.poll_failures in (None, 0), (
        "a permanent fault must not burn grace runs meant for a transient one"
    )
    assert len(result.calls) == 1, "a 4xx is not retried"


# --- webhook recovery, and what is no longer called every run ------------------------------

def test_a_webhook_is_deleted_and_the_poll_retried(run_callbacks):
    """Telegram refuses getUpdates while a webhook is active and says so with a 409, so the
    condition announces itself rather than needing to be polled for."""
    result = run_callbacks(
        updates=[callback_update(7, HASH)],
        start_state=one_pending(),
        poll_errors=[http_error(409)],
        webhook_url="https://example.com/hook",
    )

    assert result.calls[:4] == [
        "getUpdates", "getWebhookInfo", "deleteWebhook", "getUpdates",
    ]
    assert len(result.appended) == 1, "the tap behind the webhook is processed on the retry"
    assert result.code == 0


def test_a_409_with_no_webhook_is_transient_rather_than_red(run_callbacks):
    """409 also means a second getUpdates in flight. There is nothing to delete and nothing to
    fix, and a run a minute going red over it is the noise this change exists to remove."""
    result = run_callbacks(
        start_state=callback_bot_state(),
        poll_errors=[http_error(409), http_error(409)],
        webhook_url="",
    )

    assert result.code == 0
    assert result.poll_failures == 1
    assert "no webhook set" in result.log
    assert "deleteWebhook" not in result.calls, "nothing was set, so nothing to delete"


def test_a_webhook_that_will_not_delete_escalates_by_count(run_callbacks):
    """Not red on the spot: if the deletion does take, the very next run recovers on its own."""
    result = run_callbacks(
        start_state=callback_bot_state(),
        poll_errors=[http_error(409), http_error(409)],
        webhook_url="https://example.com/hook",
    )

    assert result.calls.count("deleteWebhook") == 1
    assert result.code == 0
    assert result.poll_failures == 1


def test_a_healthy_run_makes_exactly_one_telegram_call_before_the_taps(run_callbacks):
    """getMe only ever printed the bot identity, and getWebhookInfo guarded a one-time condition.
    Both ran unguarded on every dispatch -- 2,880 calls a day, each one a way for a stalled
    Telegram to fail the job before a tap was read. That is what took the runs down."""
    result = run_callbacks(start_state=callback_bot_state())

    assert result.calls == ["getUpdates"]
    assert "getMe" not in result.calls
    assert "getWebhookInfo" not in result.calls


# --- budgets ------------------------------------------------------------------------------

def test_polling_is_bounded_more_tightly_than_the_dispatch_interval(run_callbacks):
    """A run that outlives the one-minute dispatch interval gets the queued dispatch behind it
    cancelled, because only one run may sit pending in the repo-watcher-state group."""
    result = run_callbacks(start_state=callback_bot_state(), poll_errors=STALL)

    polls = result.timeout_for("getUpdates")
    assert polls, "the run polled"
    assert all(0 < t <= 10 for t in polls), f"unexpected poll timeouts: {polls}"
    assert len(polls) <= 2, f"too many attempts to fit inside a minute: {len(polls)}"


def test_the_mutation_path_keeps_the_full_budget(run_callbacks):
    """Only polling was shortened. A Sheets append happens after the tap is committed to, so
    giving up on it early would trade a slow run for a lost row."""
    result = run_callbacks(updates=[callback_update(7, HASH)], start_state=one_pending())

    assert result.timeout_for("sheets:append") == [30]
    assert result.timeout_for("editMessageText") == [30]
