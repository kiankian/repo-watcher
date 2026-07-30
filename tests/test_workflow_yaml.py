"""Structural assertions on watch-files.yml itself.

Job- and step-level `if:` guards are evaluated by GitHub Actions, not by the python block, so the
harness in conftest.py cannot reach them. These are the only way to keep them covered.
"""
import re

import yaml

from conftest import WORKFLOW, workflow_block


def _code_lines(script):
    """Non-blank, non-comment lines. Both files discuss commands in comments, so a naive
    substring search finds the prose rather than the command."""
    return [
        line.strip() for line in script.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def load_workflow():
    parsed = yaml.safe_load(WORKFLOW.read_text())
    # PyYAML resolves the `on:` key to boolean True under YAML 1.1; Actions is fine with it.
    return parsed


def watch_steps():
    return load_workflow()["jobs"]["watch"]["steps"]


def step_names():
    return [step.get("name") for step in watch_steps()]


def test_dry_run_is_an_input_defaulting_to_false():
    """cron-job.org posts {"ref":"main"} with no inputs, so the default is what keeps the live
    trigger behaving as a real run."""
    workflow = load_workflow()
    trigger = workflow.get("on") or workflow.get(True)

    dry_run = trigger["workflow_dispatch"]["inputs"]["dry_run"]
    assert dry_run["default"] is False
    assert dry_run["type"] == "boolean"


def test_the_callback_job_is_gated_on_dry_run():
    """process_applies appends to the Google Sheet, edits Telegram messages and commits
    .bot_state.json. Ungated, a dry run mutates external state -- the opposite of a rehearsal."""
    guard = load_workflow()["jobs"]["process_applies"]["if"]

    assert "dry_run" in guard, "a dry run must not process callbacks"
    assert "always()" in guard, "but a real run still processes taps when the watch job fails"


def test_the_commit_step_is_gated_on_dry_run():
    commit = next(s for s in watch_steps() if s["name"] == "Commit updated state")

    assert "dry_run" in commit["if"]


def test_the_healthcheck_ping_runs_after_the_state_push():
    """The ping is an external dead-man switch, so it has to mean the whole stateful run
    completed. Pinging from inside the watcher reported healthy even when the push exhausted its
    retries, leaving delivered alerts unrecorded and due to resend."""
    names = step_names()

    assert "Ping healthcheck" in names, "the ping must be its own step"
    assert names.index("Ping healthcheck") > names.index("Commit updated state")


def test_the_ping_is_skipped_on_failure_and_on_dry_runs():
    ping = next(s for s in watch_steps() if s["name"] == "Ping healthcheck")

    assert "success()" in ping["if"], (
        "supplying any `if:` drops the implicit success() -- and not pinging after a failed push "
        "is the entire point of the step"
    )
    assert "dry_run" in ping["if"]


def test_the_watcher_block_no_longer_pings():
    """Belt and braces on the move: a leftover ping inside the python block would fire before the
    commit step and defeat the reordering.

    Checks for a *read* of the variable, not a mention -- the block still explains in a comment
    what the ping is for, and that prose should not fail the test.
    """
    assert 'os.environ.get("HEALTHCHECK_PING_URL")' not in workflow_block(0)
    assert "urlopen" not in _code_lines(workflow_block(0))[-1], "no trailing ping call"


def test_the_ping_step_tolerates_an_unset_secret():
    ping = next(s for s in watch_steps() if s["name"] == "Ping healthcheck")

    assert re.search(r'if\s+\[\s+-n\s+"\$HEALTHCHECK_PING_URL"\s+\]', ping["run"]), (
        "the ping is optional; an unset secret must be a no-op rather than a curl error"
    )


def test_the_commit_step_creates_the_log_dir_before_adding_it():
    """`git add` on a missing pathspec exits 128, which would fail the step and leave the state
    unpushed on any run that wrote no log line."""
    commit = next(s for s in watch_steps() if s["name"] == "Commit updated state")
    lines = _code_lines(commit["run"])

    mkdir = next(i for i, line in enumerate(lines) if line.startswith("mkdir -p logs"))
    add = next(i for i, line in enumerate(lines) if line.startswith("git add"))
    assert mkdir < add
    assert "logs" in lines[add], "the log dir is what needed creating"


# --- runs off the default branch cannot write state --------------------------------------

def test_a_non_default_branch_is_forced_into_dry_mode():
    """A dispatch from a feature branch carries that branch's stale state snapshot, so it
    re-alerts jobs already sent, and it cannot save what it sent -- the commit step targets the
    default branch and conflicts irreconcilably against a shallow divergent checkout. That
    combination sent three unrecorded alerts on 2026-07-30."""
    workflow = load_workflow()
    watcher = next(s for s in watch_steps() if s["name"] == "Run watcher")

    assert workflow["env"]["ON_DEFAULT_BRANCH"] == (
        "${{ github.ref_name == github.event.repository.default_branch }}"
    )
    dry = watcher["env"]["DRY_RUN"]
    assert "inputs.dry_run" in dry and "default_branch" in dry, (
        "the input alone is not enough: the checkbox does not render when the form is built "
        "from the default branch, which is how the real run went out un-dry"
    )


def test_state_writing_is_confined_to_the_default_branch():
    commit = next(s for s in watch_steps() if s["name"] == "Commit updated state")
    ping = next(s for s in watch_steps() if s["name"] == "Ping healthcheck")
    callbacks = load_workflow()["jobs"]["process_applies"]["if"]

    for guard, name in ((commit["if"], "commit"), (ping["if"], "ping"), (callbacks, "callbacks")):
        assert "default_branch" in guard or "ON_DEFAULT_BRANCH" in guard, (
            f"{name} step must not run off the default branch"
        )
