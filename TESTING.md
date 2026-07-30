# Testing changes without missing new jobs

The primary acceptance criterion for this repository is **recall**: after a source
has been bootstrapped, every genuinely new in-scope listing must produce exactly
one attempted Telegram alert. Avoiding duplicate alerts matters, but it is
secondary to avoiding a false negative.

This guide tests the code embedded in
`.github/workflows/watch-files.yml` directly. Do not copy parser functions into a
test module: a copied parser can pass after the production parser has broken.
The commands below extract and compile the workflow's actual heredocs, then load
the actual watcher configuration and parser functions for isolated fixtures.
They never contact GitHub, Telegram, or Google Sheets and never modify the two
committed state files.

Copying applies to call sites too, not just function bodies. A test that calls a
production parser with arguments production never passes will pass while
production is broken, so section 3 derives every parser call from one `effective()`
helper that mirrors the workflow's real call sites, and pins those call sites so
that editing them fails the run instead of drifting silently.

Read section 9 before treating a green run as safety. Every check here is
triggered by editing this repository, while the largest recall risks — no run
happening at all, upstream reformatting with no local commit, a source silently
parsing zero rows for weeks — are not, and none of these checks are enforced by
CI.

## 1. Define the change's risk before editing

Write down the answers in the pull request before choosing tests:

1. Which `WATCHERS` entries, sections, and columns can the change affect?
2. Is each affected source snapshot-deduplicated or cumulative-URL-deduplicated?
3. What represents one distinct opening for that source?
4. What old state shapes must continue to work?
5. Can the change alter bootstrap, empty-parse, SHA-advance, URL, or alert-send
   behavior?

For parser or scope changes, compare representative upstream content with
`PARSING_REFERENCE.md` and update that file and `README.md` in the same commit.
Capture upstream samples as small, sanitized local fixtures; do not make a live
workflow run the first test.

## 2. Required checks for every commit

Run from the repository root:

```sh
set -eu

# The workflow must remain valid YAML and manually dispatchable only.
ruby -e 'require "yaml"; YAML.parse_file(".github/workflows/watch-files.yml"); puts "YAML parses OK"'
ruby - <<'RUBY'
require "yaml"
doc = YAML.parse_file(".github/workflows/watch-files.yml").to_ruby
# YAML 1.1 treats the key `on` as boolean true, so accept either spelling.
trigger = doc.fetch("on", doc[true])
abort("unexpected workflow triggers: #{trigger.inspect}") unless trigger == {"workflow_dispatch" => nil}
puts "workflow_dispatch is the only trigger"
RUBY

# Compile both inline Python programs exactly as checked in.
rm -rf /tmp/repo-watcher-heredocs
mkdir -p /tmp/repo-watcher-heredocs
python3 - <<'PY'
from pathlib import Path
import re

text = Path(".github/workflows/watch-files.yml").read_text()
blocks = re.findall(r"^          python - <<'PY'\n(.*?)^          PY$", text, re.M | re.S)
assert len(blocks) == 2, f"expected 2 Python heredocs, found {len(blocks)}"
out = Path("/tmp/repo-watcher-heredocs")
for number, block in enumerate(blocks, 1):
    lines = [line[10:] if line.startswith("          ") else line for line in block.splitlines()]
    (out / f"inline-{number}.py").write_text("\n".join(lines) + "\n")
print(f"extracted {len(blocks)} inline programs")
PY
python3 -m py_compile /tmp/repo-watcher-heredocs/inline-1.py
python3 -m py_compile /tmp/repo-watcher-heredocs/inline-2.py

# Runtime data is inspected, never rewritten.
python3 -m json.tool .watcher_state.json >/dev/null
python3 -m json.tool .bot_state.json >/dev/null

# Detect whitespace problems and accidental production-state edits. Check the index as
# well as the working tree: `git diff --exit-code` compares the working tree against the
# index, so a state edit that is already staged exits 0, and `git status --short` prints
# it but still succeeds. Once such an edit is committed both are clean, which is why
# section 5 additionally diffs every commit against its parent.
git diff --check
git status --short
git diff -- . ':(exclude).watcher_state.json' ':(exclude).bot_state.json'
git diff --exit-code -- .watcher_state.json .bot_state.json
git diff --cached --exit-code -- .watcher_state.json .bot_state.json
```

## 3. Run the production parser against missed-job fixtures

The following self-contained regression runner imports `WATCHERS` and parser
functions from the extracted **production** watcher program. It exercises every
enabled watcher entry and the failure modes most likely to hide a new job:

- marker boundaries and the configured column indexes;
- HTML `<th>` header rows and Markdown header/separator rows;
- `↳` company inheritance;
- HTML entities and both supported link forms;
- preservation of apply-URL query strings;
- the zero-row blackout an upstream column drop produces;
- snapshot versus cumulative-URL identity, and two same-looking openings that
  differ only by URL, **as modelled by `classify()`**.

`classify()` is a hand-written model of the alert decision, not the production
code path, because that decision lives inline in the watcher loop and cannot be
imported. This runner therefore proves parsing and identity, and nothing about
state. Bootstrap, unchanged-SHA skipping, empty-parse preservation and send
failures are executed for real against the production loop in section 4; do not
read a `PASS` here as evidence about any of them.

Create and run the temporary test (it writes only under `/tmp`):

```sh
cat >/tmp/repo-watcher-regression.py <<'PY'
from pathlib import Path
import ast
import html
import re

SOURCE = Path("/tmp/repo-watcher-heredocs/inline-1.py")
tree = ast.parse(SOURCE.read_text(), filename=str(SOURCE))
wanted_assignments = {"WATCHERS", "URL_CAP"}
wanted_functions = {
    "strip_html", "extract_apply_url", "extract_section",
    "parse_html_rows", "parse_markdown_rows", "row_key",
}
nodes = []
for node in tree.body:
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(t, ast.Name) and t.id in wanted_assignments for t in targets):
            nodes.append(node)
    elif isinstance(node, ast.FunctionDef) and node.name in wanted_functions:
        nodes.append(node)
module = ast.Module(body=nodes, type_ignores=[])
ns = {"re": re, "html": html}
exec(compile(module, str(SOURCE), "exec"), ns)

WATCHERS = ns["WATCHERS"]
extract_section = ns["extract_section"]
parse_html_rows = ns["parse_html_rows"]
parse_markdown_rows = ns["parse_markdown_rows"]
row_key = ns["row_key"]

enabled = [w for w in WATCHERS if w.get("enabled", True)]
assert len(WATCHERS) == 8, f"review expected watcher count: {len(WATCHERS)}"
assert len(enabled) == 6, f"review enabled scope: {len(enabled)}"

# --- Pin the production call sites -----------------------------------------------------
# classify() below re-expresses the alert decision, which lives inline in the watcher loop
# and therefore cannot be imported. Pin the exact production lines so that editing them
# fails this runner instead of silently drifting away from the copy in this guide.
PRODUCTION_LINES = (
    # cumulative-URL identity
    "new_listings = [r for r in curr_rows if r[4] and r[4] not in seen_set]",
    # snapshot identity
    "prev_keys = {row_key(r) for r in prev_rows}",
    "new_listings = [r for r in curr_rows if row_key(r) not in prev_keys]",
    # snapshot empty-parse guard -- conditional on prev_rows being non-empty (see section 4)
    "if prev_rows and not curr_rows:",
    # snapshot parser call -- passes NO column arguments
    "curr_rows = parse_markdown_rows(section_text)",
    # per-watcher commit fetch -- still unguarded, so one bad repo aborts the whole run
    '''latest = get_json(f"https://api.github.com/repos/{repo_full}/commits/{w['branch']}")''',
)
# Match whole stripped lines, never substrings: a substring check happily accepts appended
# code (a `[:25]` burst cap on the end of a dedup line would slip straight through).
source_lines = {line.strip() for line in SOURCE.read_text().splitlines()}
for pinned in PRODUCTION_LINES:
    assert pinned in source_lines, (
        f"production logic changed; re-derive parse()/classify() before trusting this run: {pinned!r}"
    )

# The snapshot path calls parse_markdown_rows(section_text) with no arguments, so every
# per-watcher column override is IGNORED in production. A snapshot markdown watcher that
# declares one is mis-parsed in production while a config-driven test still passes, so fail
# loudly here. Disabled watchers are checked too: enabling one is a one-line flip.
SNAPSHOT_IGNORED_KEYS = ("role_col", "loc_col", "apply_col", "term_col",
                         "default_term", "min_cells", "strip_bold")
for w in WATCHERS:
    if w["parser"] == "markdown" and w.get("dedup") != "cumulative_url":
        ignored = [k for k in SNAPSHOT_IGNORED_KEYS if k in w]
        assert not ignored, (
            f"{w['state_key']}: the snapshot path ignores {ignored}, so production reads the "
            "parser's default columns and can drop or mis-key every row. Either pass this "
            "watcher's config at the snapshot parse_markdown_rows() call in watch-files.yml, "
            'or give the source "dedup": "cumulative_url" (that path does pass the config).'
        )

def md_row(cells):
    return "| " + " | ".join(cells) + " |"

def effective(w):
    """The parser arguments production actually uses for this watcher.

    Fixtures and parsing are both driven from this one place, so a fixture can never be
    built against columns production does not actually read. Mirror watch-files.yml here.
    """
    if w["parser"] == "html":
        return {"role_col": 1, "loc_col": 2,
                "apply_col": w["apply_col"], "term_col": w["term_col"],
                "default_term": w["default_term"], "strip_bold": False,
                # parse_html_rows derives its own minimum cell count.
                "min_cells": max(w["apply_col"], w["term_col"] or 0) + 1}
    if w.get("dedup") == "cumulative_url":
        # The cumulative path passes the full per-watcher column config.
        return {"role_col": w.get("role_col", 1), "loc_col": w.get("loc_col", 2),
                "apply_col": w["apply_col"], "term_col": w["term_col"],
                "default_term": w["default_term"], "min_cells": w.get("min_cells", 5),
                "strip_bold": w.get("strip_bold", False)}
    # The snapshot path passes nothing, so parse_markdown_rows' own defaults win.
    return {"role_col": 1, "loc_col": 2, "apply_col": 3, "term_col": 4,
            "default_term": None, "min_cells": 5, "strip_bold": False}

def fixture(w, rows):
    """Build a minimal source file using this production watcher's markers."""
    if w["parser"] == "html":
        # Real upstream tables open with a <th> header row. parse_html_rows matches only
        # <td>, so the header must contribute no listing; including it means a regression
        # that starts matching <th> fails the row-count assertions below instead of
        # silently seeding and alerting a bogus "Company / Role / Location" row.
        e = effective(w)
        header = "<tr>" + "".join(f"<th>H{i}</th>" for i in range(e["min_cells"])) + "</tr>"
        body = header + "\n" + "\n".join(
            "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"
            for cells in rows
        )
    else:
        width = len(rows[0])
        body = md_row(["Company"] + [f"H{i}" for i in range(1, width)]) + "\n"
        body += md_row(["---"] * width) + "\n"
        body += "\n".join(md_row(cells) for cells in rows)
    return f"noise\n{w['section_start']}\n{body}\n{w['section_end']}\nout-of-scope"

def cells_for(w, company, role, location, url):
    e = effective(w)
    width = max(e["min_cells"], e["apply_col"] + 1, (e["term_col"] or 0) + 1)
    cells = [""] * width
    cells[0], cells[e["role_col"]], cells[e["loc_col"]] = company, role, location
    if e["term_col"] is not None:
        cells[e["term_col"]] = "Summer &amp; Fall 2099"
    if w["parser"] == "html" or "speedyapply/" in w["state_key"]:
        cells[e["apply_col"]] = f'<a href="{url}">Apply</a>'
    else:
        cells[e["apply_col"]] = f"[Apply]({url})"
    return cells

def parse(w, text):
    """Call the parser exactly the way the production loop calls it for this watcher."""
    section = extract_section(text, w["section_start"], w["section_end"])
    if w["parser"] == "html":
        return parse_html_rows(section, w["term_col"], w["apply_col"], w["default_term"])
    if w.get("dedup") == "cumulative_url":
        return parse_markdown_rows(
            section,
            role_col=w.get("role_col", 1), loc_col=w.get("loc_col", 2),
            apply_col=w["apply_col"], term_col=w["term_col"],
            default_term=w["default_term"], min_cells=w.get("min_cells", 5),
            strip_bold=w.get("strip_bold", False),
        )
    # No keyword arguments on purpose: the snapshot path passes none.
    return parse_markdown_rows(section)

def classify(w, previous, current):
    """Mirror the workflow's two intentional listing-identity models."""
    if w.get("dedup") == "cumulative_url":
        seen = set(previous)
        return [r for r in current if r[4] and r[4] not in seen]
    previous_keys = {row_key(r) for r in previous}
    return [r for r in current if row_key(r) not in previous_keys]

for w in enabled:
    e = effective(w)
    url1 = "https://jobs.example.test/apply?job=old&source=board"
    url2 = "https://jobs.example.test/apply?job=NEW&source=board"
    company = "**Example &amp; Co**" if e["strip_bold"] else "Example &amp; Co"
    old_cells = cells_for(w, company, "Software Engineer Intern", "Remote", url1)
    new_cells = cells_for(w, "↳", "Platform Engineer Intern", "New York, NY", url2)
    parsed_old = parse(w, fixture(w, [old_cells]))
    parsed_both = parse(w, fixture(w, [old_cells, new_cells]))

    assert len(parsed_old) == 1, (w["state_key"], parsed_old)
    assert len(parsed_both) == 2, (w["state_key"], parsed_both)
    assert parsed_old[0][0] == "Example & Co", (w["state_key"], parsed_old)
    assert parsed_both[1][0] == "Example & Co", (w["state_key"], parsed_both)
    assert parsed_both[1][4] == url2, (w["state_key"], parsed_both)

    # The term must come from the column production actually reads, entity-decoded.
    expected_term = e["default_term"] if e["term_col"] is None else "Summer & Fall 2099"
    assert parsed_both[1][3] == expected_term, (w["state_key"], parsed_both)

    # An empty apply URL is a dead "Apply" button and an unloggable job. The snapshot path
    # would still alert on such a row, so assert the URL rather than just the row count.
    assert parsed_old[0][4] == url1, (w["state_key"], parsed_old)

    previous = [url1] if w.get("dedup") == "cumulative_url" else parsed_old
    new_jobs = classify(w, previous, parsed_both)
    assert len(new_jobs) == 1 and new_jobs[0][4] == url2, (w["state_key"], new_jobs)

    # Cumulative sources must not collapse distinct openings with identical text.
    if w.get("dedup") == "cumulative_url":
        twin = list(parsed_both[0])
        twin[4] = "https://jobs.example.test/apply?job=SECOND-ID"
        assert classify(w, [url1], [parsed_both[0], twin]) == [twin]

    # Missing markers/empty parses must be detectable so callers preserve old state/SHA.
    assert parse(w, "no configured markers here") == []

    # Upstream dropping a single column pushes rows below the effective min_cells, so the
    # parser returns nothing and the empty-guard then skips this source on EVERY later run,
    # logging only a WARNING. This is the silent-blackout mechanism section 9 monitors for;
    # assert it here so nobody mistakes "state preserved" for "still catching jobs".
    if w["parser"] == "markdown":
        narrow = cells_for(w, company, "Software Engineer Intern", "Remote", url1)[:-1]
        assert parse(w, fixture(w, [narrow])) == [], (w["state_key"], "expected blackout")

print(f"PASS: every one of {len(enabled)} enabled watcher entries detected its injected job")
PY
python3 /tmp/repo-watcher-regression.py
```

Treat a watcher-count assertion failure as a required test review, not as a
number to update mechanically. A new watcher needs a representative fixture and
an explicit scope decision. Disabled watchers should also be tested before being
enabled; temporarily change `enabled` only in a disposable worktree, run the
fixture, and discard that worktree.

## 4. Stateful two-run test: prove the alert decision

Parser success alone is insufficient: a correct parser still misses jobs if the
state machine around it decides wrongly. The table below is the specification,
and the runner after it executes the **real** watcher loop against a stubbed
network to assert the rows it can reach. For every affected source, reason through
two consecutive runs with an isolated state object:

| Scenario | Expected alert attempts | Expected state/SHA result |
|---|---:|---|
| No saved state + existing rows | 0 | silent bootstrap seeds rows/URLs and SHA |
| Saved state + one injected in-scope row | 1 | new snapshot/URL and SHA saved |
| Same SHA and initialized state | 0 | network/parser skipped |
| Changed SHA, same listings | 0 | SHA advances; identities remain known |
| Changed SHA, zero rows, **non-empty** prior rows | 0 | old state and old SHA preserved |
| Changed SHA, zero rows, prior rows `[]` (snapshot) | 0 | ⚠️ guard does **not** fire; SHA advances with `rows: []` |
| Changed SHA, zero rows (cumulative) | 0 | old state and old SHA preserved (guard is unconditional) |
| Existing row removed | 0 | snapshot drops it; cumulative seen set retains URL |
| Removed row returns | snapshot: 1; cumulative: 0 | follows the documented identity model |
| Alert send fails | 0 successful | ⚠️ current code still advances watcher state |
| One watcher's commit fetch returns 404/403 | 0 for **all** watchers | ⚠️ run aborts; no state persisted at all |

The two empty-parse rows differ because the guards differ. The cumulative path
guards unconditionally, but the snapshot guard is `if prev_rows and not
curr_rows`, so an empty `rows: []` snapshot (which bootstrap can seed during a
quiet window) is falsy and the SHA advances anyway. That is not itself a missed
job — the next non-empty parse re-alerts everything — but it means "state
preserved" is not a guarantee you can assume for the snapshot path.

The `Alert send fails` row is the single worst recall bug in the repo: the send
error is caught and skipped, yet the row/URL is still written into state at the
end of the watcher block, so the job is permanently marked seen and **never
retried**. Any change touching send or state ordering must include a mocked
send-failure test and must not make this failure mode worse; a deliberate
retry/outbox redesign should add migration and recovery tests.

The last row is untested and unbounded: the per-watcher commit fetch is not
wrapped in `try`/`except`, and `urlopen_with_retry` re-raises any HTTP status
below 500. A renamed branch, a repo going private, or a 404 therefore aborts the
whole job, so every watcher after the failing one is never checked. Because both
state files are written only after the loop finishes, that abort also discards
the `pending` entries for alerts **already sent during that run** — those
Telegram messages keep an `Apply` button whose callback hash no longer resolves,
so the job cannot be logged even though it was delivered. When changing the loop,
fetch handling, or state persistence, test with one watcher forced to raise and
require that later watchers still run and that already-sent alerts keep a
resolvable `pending` entry.

For an affected cumulative source, inject **two rows with identical company,
role, location, and category but different full URLs** and require two alert
attempts. For a snapshot source, inject a row that changes only a volatile field
outside `(company, role, location, term)` and require zero alerts. Always include
a URL whose job ID is only in its query string.

### Executable state-transition runner

The section 3 runner cannot reach any of this: it imports parser functions, while
the state machine lives in the loop. This runner instead executes the entire
production program with `urllib.request.urlopen` replaced and the working
directory moved to a temporary path, so the relative state files it writes
(`.watcher_state.json`, `.bot_state.json`) land there. Nothing contacts GitHub,
Telegram, or Sheets, and the committed state files are never opened. It reuses
section 3's fixture builders, so both runners describe watchers identically —
which also means section 3's assertions run first, and its `PASS` line prints
before this one.

```sh
cat >/tmp/repo-watcher-loop.py <<'PY'
import importlib.util
import io
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

SOURCE = Path("/tmp/repo-watcher-heredocs/inline-1.py")
RUNNER = Path("/tmp/repo-watcher-regression.py")
WORK = Path("/tmp/repo-watcher-loop")

spec = importlib.util.spec_from_file_location("rwr", RUNNER)
rwr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rwr)
enabled, fixture, cells_for = rwr.enabled, rwr.fixture, rwr.cells_for

def body_for(owner, repo, path, rows_per_watcher):
    """Every in-scope section that shares one upstream file (speedyapply keeps three)."""
    parts = []
    for w in enabled:
        if (w["owner"], w["repo"], w["file"]) == (owner, repo, path):
            cells = [cells_for(w, "Example Co", f"SWE Intern {i}", "Remote",
                               f"https://jobs.example.test/apply?job={i}")
                     for i in range(rows_per_watcher)]
            parts.append(fixture(w, cells) if cells else "markers removed upstream")
    assert parts, f"no enabled watcher for {owner}/{repo}/{path}"
    return "\n".join(parts)

def run(tag, state, sha, rows_per_watcher, tg_fail=False, sha_404_for=None):
    workdir = WORK / tag
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / ".watcher_state.json").write_text(json.dumps(state, indent=2) + "\n")
    (workdir / ".bot_state.json").write_text(json.dumps(
        {"telegram": {"last_update_id": 0}, "pending": {}}, indent=2) + "\n")
    sent, fetched = [], []

    def fake_urlopen(req, *args, **kwargs):
        url = req.full_url
        m = re.match(r"https://api\.github\.com/repos/([^/]+)/([^/]+)/commits/(.+)", url)
        if m:
            if sha_404_for and m.group(1) == sha_404_for:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return io.BytesIO(json.dumps({"sha": sha}).encode())
        m = re.match(r"https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)", url)
        if m:
            owner, repo, _, path = m.groups()
            fetched.append(path)
            return io.BytesIO(body_for(owner, repo, path, rows_per_watcher).encode())
        if "api.telegram.org" in url:
            sent.append(json.loads(req.data.decode()))
            if tg_fail:  # 4xx, so urlopen_with_retry re-raises without sleeping
                raise urllib.error.HTTPError(url, 400, "Bad Request", {}, None)
            return io.BytesIO(json.dumps({"ok": True, "result": {
                "message_id": len(sent), "chat": {"id": -100}}}).encode())
        raise AssertionError(f"unexpected URL: {url}")

    real_urlopen, cwd = urllib.request.urlopen, os.getcwd()
    urllib.request.urlopen = fake_urlopen
    os.environ.update(GITHUB_TOKEN="fake", TELEGRAM_BOT_TOKEN="fake",
                      TELEGRAM_CHAT_ID="-100")
    os.chdir(workdir)
    raised = None
    try:
        exec(compile(SOURCE.read_text(), str(SOURCE), "exec"), {"__name__": "__main__"})
    except BaseException as exc:  # an unguarded watcher fetch aborts the whole program
        raised = exc
    finally:
        urllib.request.urlopen = real_urlopen
        os.chdir(cwd)
    return {"sent": sent, "fetched": fetched, "raised": raised,
            "state": json.loads((workdir / ".watcher_state.json").read_text()),
            "pending": json.loads((workdir / ".bot_state.json").read_text())["pending"]}

N = len(enabled)

# Silent bootstrap: no saved state seeds every watcher and alerts nothing.
boot = run("boot", {}, "sha1", 1)
assert boot["raised"] is None, boot["raised"]
assert boot["sent"] == [], f"bootstrap must be silent: {boot['sent']}"
assert len(boot["state"]) == N, boot["state"]

# One genuinely new row per watcher produces exactly one alert each -- the core
# recall assertion, made against the real loop rather than a model of it.
grew = run("grew", boot["state"], "sha2", 2)
assert len(grew["sent"]) == N, f"expected {N} alerts, got {len(grew['sent'])}"
assert len(grew["pending"]) == N, grew["pending"]

# Unchanged SHA skips the network and the parser entirely.
same = run("same", grew["state"], "sha2", 2)
assert same["sent"] == [] and same["fetched"] == [], same

# An empty parse preserves prior state byte-for-byte, including every last_sha.
empty = run("empty", grew["state"], "sha3", 0)
assert empty["sent"] == [], empty["sent"]
assert empty["state"] == grew["state"], "empty parse overwrote good state"

# A failed send is a PERMANENT miss: no pending entry is recorded, yet the SHA still
# advances and the row/URL is stored as seen, so the next run never retries it. The
# assertions below encode the bug as it stands; fixing it must change them.
lost = run("lost", grew["state"], "sha4", 3, tg_fail=True)
assert len(lost["sent"]) == N, len(lost["sent"])
assert lost["pending"] == {}, lost["pending"]
advanced = [k for k, v in lost["state"].items() if v.get("last_sha") == "sha4"]
assert len(advanced) == N, f"expected the known send-failure bug, got {advanced}"

# One unreachable repo aborts the whole program: later watchers never run, and no
# state is written at all, so alerts already sent in that run lose their pending
# entries and their Apply buttons stop resolving.
dead = run("dead", grew["state"], "sha5", 3, sha_404_for=enabled[0]["owner"])
assert isinstance(dead["raised"], urllib.error.HTTPError), dead["raised"]
assert dead["sent"] == [], dead["sent"]
assert dead["state"] == grew["state"], "expected no state write after an abort"

print(f"PASS: real loop executed; {N} watchers bootstrapped silently, alerted once each, "
      "skipped on unchanged SHA, preserved state on empty parse, and reproduced both "
      "known failure modes")
PY
python3 /tmp/repo-watcher-loop.py
```

The last two scenarios assert bugs rather than correct behavior, and say so. They
are here because an unasserted bug silently becomes the baseline: if either is
fixed, these assertions must be inverted in the same commit, which is exactly the
review conversation that should happen. Update the runner in the same commit as
any change to the loop, fetch handling, send path, or state persistence.

## 5. Run every new commit, not only the final checkout

A later commit can hide a regression introduced earlier, and reviewers may test
or revert commits independently. The loop below checks out every commit after a
chosen base into a disposable worktree, runs that commit's workflow syntax and
production-parser regression, then removes the worktree.

First save the runners from sections 3 and 4 somewhere outside the worktree (they
are already `/tmp/repo-watcher-regression.py` and `/tmp/repo-watcher-loop.py`).
Then run:

```sh
set -eu
BASE="${BASE:-origin/main}"
ROOT=$(git rev-parse --show-toplevel)
RUNNER=/tmp/repo-watcher-regression.py
LOOP_RUNNER=/tmp/repo-watcher-loop.py

for commit in $(git rev-list --reverse "$BASE"..HEAD); do
  worktree=$(mktemp -d /tmp/repo-watcher-commit.XXXXXX)
  git worktree add --detach "$worktree" "$commit" >/dev/null
  echo "== testing $commit =="
  (
    cd "$worktree"
    ruby -e 'require "yaml"; YAML.parse_file(".github/workflows/watch-files.yml")'
    rm -rf /tmp/repo-watcher-heredocs
    mkdir -p /tmp/repo-watcher-heredocs
    python3 - <<'PY'
from pathlib import Path
import re
text = Path(".github/workflows/watch-files.yml").read_text()
blocks = re.findall(r"^          python - <<'PY'\n(.*?)^          PY$", text, re.M | re.S)
assert len(blocks) == 2
out = Path("/tmp/repo-watcher-heredocs")
for i, block in enumerate(blocks, 1):
    lines = [line[10:] if line.startswith("          ") else line for line in block.splitlines()]
    (out / f"inline-{i}.py").write_text("\n".join(lines) + "\n")
PY
    python3 -m py_compile /tmp/repo-watcher-heredocs/inline-1.py /tmp/repo-watcher-heredocs/inline-2.py
    python3 "$RUNNER"
    python3 "$LOOP_RUNNER"
    python3 -m json.tool .watcher_state.json >/dev/null
    python3 -m json.tool .bot_state.json >/dev/null
    # Reject state edits that are already committed, which the working-tree and index
    # checks in section 2 cannot see.
    git diff --exit-code "$commit^" "$commit" -- .watcher_state.json .bot_state.json
  )
  git worktree remove --force "$worktree"
done
```

Use the actual merge base when the branch does not directly start at
`origin/main`:

```sh
BASE=$(git merge-base origin/main HEAD)
```

If a commit intentionally changes watcher count, layout, or scope, update the
runner as part of that same commit so the commit remains independently testable.
Note that a `/tmp` runner is a local convenience only: it survives neither a fresh
clone nor a CI job, so per-commit regression coverage lasts exactly as long as the
current session. Versioning the runner in the repository is the prerequisite for
this loop meaning anything to anyone else (see section 9).

## 6. Upstream contract check

Run this whenever `WATCHERS`, markers, or parsing changes — **and on a schedule
even when nothing in this repository changed**. The fixtures in section 3 are
generated from each watcher's own configuration, so they are self-consistent by
construction and cannot detect an upstream format change. Upstream reformatting
with zero local commits is the most common way this watcher silently stops
catching jobs, and it is the one failure mode no commit-time test can see.

Fetch upstream data read-only at an exact commit SHA. Never run the workflow
program itself. For each affected watcher:

1. Resolve the configured branch to a SHA through the GitHub API.
2. Fetch the configured file from `raw.githubusercontent.com` at that SHA.
3. Save it under `/tmp`, not in the repository.
4. Confirm both markers occur in the intended order.
5. Count source rows inside the section and parsed rows. Explain every skipped
   row (normally only header/separator rows).
6. Inspect the first, middle, and last row plus continuation rows and unusual
   links. Confirm the full apply URL survives byte-for-byte.
7. Repeat against a second recent SHA containing a known addition, seed state
   from the older sample, and require the known addition in `new_listings`.

Do not equate “the parser returned at least one row” with success. A wrong end
marker or shifted column can still return a non-empty subset and silently miss
jobs. Record the two SHAs, source/parsed counts, expected new identities, and
actual new identities in the pull request. Sanitize fixtures and output; tokens
and credentials must never appear in commands, logs, or committed files.

## 7. Callback-path checks

Changes below alert creation can also make a caught job unusable. If touching
hashes, pending metadata, callback parsing, or Sheets rows, use fake Telegram
updates and a fake Sheets append function and verify:

- each alert's `apply:<12 hex chars>` callback maps to exactly one pending job;
- two distinct apply URLs do not overwrite one another's pending entry;
- an irrelevant chat or malformed callback cannot append a row;
- one valid callback appends once, edits the correct message, advances
  `last_update_id`, and cannot append twice on replay;
- absent/invalid credentials fail without exposing their values.

Never run the callback heredoc locally with production environment variables.

## 8. Final review and pull-request evidence

Before committing, inspect the full patch and explicitly confirm:

- `workflow_dispatch` is still the only trigger (see section 9: this is an
  invariant of the current design, **not** evidence that runs are happening);
- bootstrap remains silent;
- empty parses cannot replace good state or advance the SHA, allowing for the
  snapshot-path exception in section 4;
- all intended sections remain in scope and excluded categories remain excluded;
- query strings and `↳` inheritance survive parsing;
- cumulative sources retain cumulative URL dedup and the cap is safely above the
  upstream churn window;
- `.watcher_state.json` and `.bot_state.json` have no incidental changes;
- parser/scope/state changes are reflected in both `README.md` and
  `PARSING_REFERENCE.md`.

In the pull request, list every exact command run and its result. Also list tests
not run: the live end-to-end workflow is intentionally excluded because it can
send Telegram messages, append to Google Sheets, and commit production state.
Before pushing, rebase with `git pull --rebase origin main`; never force-push or
discard newer runner state.

## 9. What this guide does not cover

Everything above is triggered by someone editing this repository. The largest
recall risks are not, so passing every check in this guide is necessary but not
sufficient. These gaps are open, and a change that closes one should be reviewed
as a recall improvement rather than a refactor.

**Nothing here is enforced.** There is no CI workflow running any of it, no
`tests/` directory, and both runners live in `/tmp`, so they are discarded between
sessions and cannot regression-test anything over time. Their protection is
exactly as strong as the discipline of whoever edits next, which is a weak
guarantee in a repository that is largely edited by agents. Versioning both
runners in-repo and running them from a path-filtered workflow on code changes
would make these checks real; until that exists, state explicitly in each pull
request that both were executed and paste their output.

**Nothing verifies that runs happen at all.** `workflow_dispatch` is the only
trigger and there is no `cron` in any workflow, so every alert depends on an
external dispatcher. If that dispatcher stops, no listing is ever fetched, no
test fails, and the repository looks healthy — total recall loss with no signal.
Section 2 asserts that `workflow_dispatch` is the only trigger and section 8
re-confirms it, but neither observes whether a run occurred. Until a heartbeat or
dead-man's-switch exists (a healthcheck ping on each completed run, alerting when
pings stop), treat "when did the workflow last succeed?" as a manual check and
answer it in any pull request that touches scheduling or dispatch.

**Nothing detects a silent blackout.** When markers move or a column disappears,
the parser returns zero rows and the empty-parse guard then skips that source on
every subsequent run, logging only a `WARNING` inside the job log. State is
preserved, which is correct, but the source has stopped catching jobs
indefinitely and nothing escalates. The section 3 blackout assertion documents
the mechanism; it cannot observe it in production. A per-watcher staleness alarm
(row count dropping sharply versus the previous run, or a source producing no new
listings for far longer than its normal cadence) is the missing control.

**Failed sends are dropped permanently.** See section 4, where the runner asserts
this against the real loop. Asserted, but not defended: no retry, no outbox, no
alert on drop.

**One failing watcher stops the rest.** See section 4's final row and its runner
scenario. No error isolation, and a mid-loop abort also loses the `pending`
entries for alerts already delivered in that run.

Both are pinned by assertions that will need inverting when they are fixed. That
is deliberate: it forces the fix to be a visible decision rather than a silent
change in behavior.
