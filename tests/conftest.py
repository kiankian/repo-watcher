"""Harness that runs the real watch-files.yml python block with the network stubbed.

The watcher logic that matters most -- "is this listing new, and may I record it as seen?" --
lives in the workflow's main loop, not in watcher/core.py. Rather than duplicate that loop in
the tests, these helpers extract the block straight out of the YAML and exec it against a
temporary checkout with urlopen replaced. A test therefore exercises the same code that runs in
CI, and a refactor of the loop cannot silently stop being covered.
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github/workflows/watch-files.yml"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Watcher under test: Simplify Summer -- html parser, term_col None, apply_col 3.
TARGET_REPO = "SimplifyJobs/Summer2026-Internships"
TARGET_KEY = f"{TARGET_REPO}#summer"
SHA_PREFIX = "deadbeef"
_sha_counter = [0]


def next_sha():
    """A fresh head sha per run.

    Chained runs must not reuse one: the loop short-circuits on last_sha == latest_sha, so a
    repeated sha would make the second run a no-op for reasons unrelated to the test.
    """
    _sha_counter[0] += 1
    return f"{SHA_PREFIX}{_sha_counter[0]:032x}"


def workflow_block(index=0):
    """Return the dedented python source of the Nth `python - <<'PY'` heredoc."""
    blocks = re.findall(r"python - <<'PY'\n(.*?)\n\s*PY\n", WORKFLOW.read_text(), re.DOTALL)
    if not blocks:
        raise AssertionError("no python heredoc found in watch-files.yml")
    return textwrap.dedent(blocks[index])


def build_html(rows):
    """Render rows into the upstream HTML shape parse_html_rows expects.

    The header row uses <th>, matching upstream -- with <td> the parser would treat it as a
    listing, which is exactly the guard `len(cells) < min_cells` relies on.
    """
    trs = "".join(
        "<tr>"
        f"<td>{r[0]}</td><td>{r[1]}</td><td>{r[2]}</td>"
        f'<td><a href="{r[4]}">Apply</a></td>'
        "</tr>"
        for r in rows
    )
    return (
        "# Intro\n\n"
        "## \U0001f4bb Software Engineering Internship Roles\n\n"
        "<table><tr><th>Company</th><th>Role</th><th>Location</th><th>Apply</th></tr>"
        f"{trs}</table>\n\n"
        "## \U0001f4f1 Product Management Internship Roles\n\n"
        "<table></table>\n"
    )


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _make_urlopen(state, html, sent, health, head_sha, fail_for=None, fail_once_at=None,
                  fetch_status=None, fetches=None, fail_all=False):
    """Stub urlopen.

    fail_for:     substring of the message text that 429s on every attempt (permanent failure)
    fail_all:     every sendMessage 429s -- job alerts *and* health notices alike, which is what
                  a real Telegram outage looks like. fail_for cannot express that: the
                  send-failure notice does not repeat the text of the job it is about.
    fail_once_at: 0-based send index that 429s once, to exercise the retry path
    fetch_status: HTTP status to raise when fetching the watched file (e.g. 404 for a rename)
    """
    calls = {"send": 0}

    def urlopen(req, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)

        if "api.github.com" in url and "/commits/" in url:
            repo = re.search(r"/repos/([^/]+/[^/]+)/commits/", url).group(1)
            if repo == TARGET_REPO:
                return _Resp(json.dumps({"sha": head_sha}).encode())
            # Report every other watcher's stored sha so it short-circuits and stays out of
            # the way; this test only drives the target watcher.
            sha = next(
                (v["last_sha"] for k, v in state.items() if k.split("#")[0] == repo), "0" * 40
            )
            return _Resp(json.dumps({"sha": sha}).encode())

        if "raw.githubusercontent.com" in url:
            if fetches is not None:
                fetches.append(url)
            if fetch_status:
                raise urllib.error.HTTPError(
                    url, fetch_status, "Not Found", {}, io.BytesIO(b"")
                )
            return _Resp(html.encode())

        if "api.telegram.org" in url and "sendMessage" in url:
            i = calls["send"]
            calls["send"] += 1
            body = json.loads(req.data.decode())
            # Health notices share sendMessage but are not job alerts; keep them apart so a
            # test asserting "no alerts" is not satisfied or broken by an operational notice.
            (health if body["text"].startswith("\u26a0\ufe0f") else sent).append(body["text"])
            if fail_all or (fail_for and fail_for in body["text"]) or i == fail_once_at:
                raise urllib.error.HTTPError(
                    url, 429, "Too Many Requests", {},
                    io.BytesIO(json.dumps(
                        {"ok": False, "parameters": {"retry_after": 0}}
                    ).encode()),
                )
            return _Resp(json.dumps({
                "ok": True,
                "result": {"message_id": 5000 + i, "chat": {"id": 7582459199}},
            }).encode())

        raise AssertionError(f"unstubbed URL: {url}")

    return urlopen


class Result:
    def __init__(self, sent, health, files, log, head_sha, logs=None, summary="",
                 step_output="", fetches=None, key=TARGET_KEY):
        self.sent = sent
        self.health = health
        self.logs = logs or {}
        self.summary = summary
        self.step_output = step_output
        # Raw-file fetches this run. Zero means a source was short-circuited without being
        # looked at, which is the difference between a real rehearsal and a no-op.
        self.fetches = fetches or []
        self.files = files
        self.log = log
        self.head_sha = head_sha
        self.state = json.loads(files[".watcher_state.json"])
        self.bot_state = json.loads(files[".bot_state.json"])
        self._key = key

    @property
    def rows(self):
        return self.state[self._key].get("rows", [])

    @property
    def seen(self):
        return self.state[self._key].get("seen", [])

    @property
    def outbox(self):
        return self.state[self._key].get("outbox", [])

    @property
    def last_sha(self):
        return self.state[self._key]["last_sha"]

    def log_records(self, prefix):
        """Parsed lines from every logs/<prefix>-*.jsonl this run produced."""
        out = []
        for name, text in sorted(self.logs.items()):
            if name.startswith(prefix):
                out += [json.loads(line) for line in text.splitlines() if line.strip()]
        return out

    def sent_matching(self, needle):
        return [t for t in self.sent if needle in t]

    def health_matching(self, needle):
        return [t for t in self.health if needle in t]

    @property
    def healthy(self):
        """The `healthy` step output that gates the external healthcheck ping."""
        for line in self.step_output.splitlines():
            if line.startswith("healthy="):
                return line.split("=", 1)[1] == "true"
        return None


@pytest.fixture
def run_watcher():
    """Return `run(upstream_rows, **kw) -> Result`, runnable repeatedly to chain runs."""

    def run(upstream_rows, start_files=None, fail_for=None, fail_once_at=None,
            drop_target_state=False, dry_run=False, capture_summary=False,
            fetch_status=None, reuse_sha=None, monotonic_step=None, fail_all=False):
        work = Path(tempfile.mkdtemp())
        try:
            for name in (".watcher_state.json", ".bot_state.json"):
                if start_files and name in start_files:
                    (work / name).write_text(start_files[name])
                else:
                    shutil.copy(FIXTURES / name.lstrip("."), work / name)
            # Mirror the checkout layout: the block imports watcher.core relative to cwd.
            shutil.copytree(REPO_ROOT / "watcher", work / "watcher")

            state_before = json.loads((work / ".watcher_state.json").read_text())
            if drop_target_state:
                # Simulate a source the watcher has never seen, to exercise silent bootstrap.
                state_before.pop(TARGET_KEY, None)
                (work / ".watcher_state.json").write_text(json.dumps(state_before))
            # reuse_sha replays against a head the previous run already recorded, so the loop
            # takes its unchanged-sha path -- the only way to test that the outbox still drains.
            head_sha = reuse_sha or next_sha()
            fetches = []
            sent, health = [], []
            saved_cwd, saved_sleep = os.getcwd(), time.sleep
            saved_monotonic = time.monotonic
            saved_urlopen = urllib.request.urlopen
            saved_stdout = sys.stdout
            buf = io.StringIO()
            try:
                os.chdir(work)
                os.environ.update(
                    GITHUB_TOKEN="test-token",
                    TELEGRAM_BOT_TOKEN="test-bot-token",
                    TELEGRAM_CHAT_ID="7582459199",
                    DRY_RUN="true" if dry_run else "false",
                )
                os.environ.pop("HEALTHCHECK_PING_URL", None)
                if capture_summary:
                    os.environ["GITHUB_STEP_SUMMARY"] = str(work / "summary.md")
                else:
                    os.environ.pop("GITHUB_STEP_SUMMARY", None)
                # `python - <<PY` puts cwd on sys.path; replicate it, and evict any cached
                # watcher module so each run imports from its own work dir.
                sys.path.insert(0, str(work))
                _evict_watcher_modules()
                os.environ["GITHUB_OUTPUT"] = str(work / "step_output.txt")
                urllib.request.urlopen = _make_urlopen(
                    state_before, build_html(upstream_rows), sent, health, head_sha,
                    fail_for, fail_once_at, fetch_status, fetches, fail_all
                )
                time.sleep = lambda _s: None  # keep send spacing from slowing the suite
                if monotonic_step:
                    # Deterministic clock: each reading advances by a fixed amount, so the
                    # run-wide send budget can be exercised without the suite actually waiting.
                    ticks = [0.0]

                    def fake_monotonic():
                        ticks[0] += monotonic_step
                        return ticks[0]

                    time.monotonic = fake_monotonic
                sys.stdout = buf
                exec(compile(workflow_block(0), "<watch-files.yml>", "exec"),
                     {"__name__": "__main__"})
            finally:
                sys.stdout = saved_stdout
                os.chdir(saved_cwd)
                if str(work) in sys.path:
                    sys.path.remove(str(work))
                _evict_watcher_modules()
                urllib.request.urlopen = saved_urlopen
                time.sleep = saved_sleep
                time.monotonic = saved_monotonic

            files = {
                name: (work / name).read_text()
                for name in (".watcher_state.json", ".bot_state.json")
            }
            logs = {
                path.name: path.read_text()
                for path in sorted((work / "logs").glob("*.jsonl"))
            } if (work / "logs").is_dir() else {}
            summary = work / "summary.md"
            step_output = work / "step_output.txt"
            return Result(sent, health, files, buf.getvalue(), head_sha, logs,
                          summary.read_text() if summary.exists() else "",
                          step_output.read_text() if step_output.exists() else "",
                          fetches=fetches)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    return run


def _evict_watcher_modules():
    for name in [k for k in sys.modules if k == "watcher" or k.startswith("watcher.")]:
        del sys.modules[name]


@pytest.fixture
def fixture_rows():
    """The frozen upstream rows for the target watcher."""
    return json.loads((FIXTURES / "watcher_state.json").read_text())[TARGET_KEY]["rows"]


@pytest.fixture
def baseline(run_watcher, fixture_rows):
    """A migration-only run: no new listings, so it establishes the post-migration `seen` size.

    Tests assert deltas against this rather than against len(fixture_rows), because migration
    also seeds identities out of bot_state, not just out of the stored rows.
    """
    result = run_watcher(fixture_rows)
    assert result.sent == [], "the baseline run must not alert"
    return result


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())
