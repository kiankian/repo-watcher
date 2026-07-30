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

# Detect whitespace problems and accidental production-state edits.
git diff --check
git status --short
git diff -- . ':(exclude).watcher_state.json' ':(exclude).bot_state.json'
git diff --exit-code -- .watcher_state.json .bot_state.json
```

## 3. Run the production parser against missed-job fixtures

The following self-contained regression runner imports `WATCHERS` and parser
functions from the extracted **production** watcher program. It exercises every
enabled watcher entry and the failure modes most likely to hide a new job:

- marker boundaries and the configured column indexes;
- HTML and Markdown headers/separators;
- `↳` company inheritance;
- HTML entities and both supported link forms;
- preservation of apply-URL query strings;
- snapshot identity versus cumulative URL identity;
- two same-looking openings with different URLs for cumulative sources;
- silent bootstrap, unchanged-SHA skipping, and empty-parse state preservation.

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

def md_row(cells):
    return "| " + " | ".join(cells) + " |"

def fixture(w, rows):
    """Build a minimal source file using this production watcher's markers."""
    if w["parser"] == "html":
        body = "\n".join(
            "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"
            for cells in rows
        )
    else:
        width = max(w.get("min_cells", 5), w.get("apply_col", 3) + 1)
        body = md_row(["Company"] + [f"H{i}" for i in range(1, width)]) + "\n"
        body += md_row(["---"] * width) + "\n"
        body += "\n".join(md_row(cells) for cells in rows)
    return f"noise\n{w['section_start']}\n{body}\n{w['section_end']}\nout-of-scope"

def cells_for(w, company, role, location, url):
    apply_col = w.get("apply_col", 3)
    width = max(w.get("min_cells", 5), apply_col + 1,
                (w.get("term_col") or 0) + 1)
    cells = [""] * width
    cells[0], cells[w.get("role_col", 1)], cells[w.get("loc_col", 2)] = company, role, location
    if w.get("term_col") is not None:
        cells[w["term_col"]] = "Summer &amp; Fall 2099"
    if w["parser"] == "html" or "speedyapply/" in w["state_key"]:
        cells[apply_col] = f'<a href="{url}">Apply</a>'
    else:
        cells[apply_col] = f"[Apply]({url})"
    return cells

def parse(w, text):
    section = extract_section(text, w["section_start"], w["section_end"])
    if w["parser"] == "html":
        return parse_html_rows(section, w["term_col"], w["apply_col"], w["default_term"])
    return parse_markdown_rows(
        section,
        role_col=w.get("role_col", 1), loc_col=w.get("loc_col", 2),
        apply_col=w.get("apply_col", 3), term_col=w.get("term_col", 4),
        default_term=w.get("default_term"), min_cells=w.get("min_cells", 5),
        strip_bold=w.get("strip_bold", False),
    )

def classify(w, previous, current):
    """Mirror the workflow's two intentional listing-identity models."""
    if w.get("dedup") == "cumulative_url":
        seen = set(previous)
        return [r for r in current if r[4] and r[4] not in seen]
    previous_keys = {row_key(r) for r in previous}
    return [r for r in current if row_key(r) not in previous_keys]

for w in enabled:
    url1 = "https://jobs.example.test/apply?job=old&source=board"
    url2 = "https://jobs.example.test/apply?job=NEW&source=board"
    company = "**Example &amp; Co**" if w.get("strip_bold") else "Example &amp; Co"
    old_cells = cells_for(w, company, "Software Engineer Intern", "Remote", url1)
    new_cells = cells_for(w, "↳", "Platform Engineer Intern", "New York, NY", url2)
    parsed_old = parse(w, fixture(w, [old_cells]))
    parsed_both = parse(w, fixture(w, [old_cells, new_cells]))

    assert len(parsed_old) == 1, (w["state_key"], parsed_old)
    assert len(parsed_both) == 2, (w["state_key"], parsed_both)
    assert parsed_old[0][0] == "Example & Co", (w["state_key"], parsed_old)
    assert parsed_both[1][0] == "Example & Co", (w["state_key"], parsed_both)
    assert parsed_both[1][4] == url2, (w["state_key"], parsed_both)

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

Parser success alone is insufficient. For every affected source, reason through
two consecutive runs with an isolated state object:

| Scenario | Expected alert attempts | Expected state/SHA result |
|---|---:|---|
| No saved state + existing rows | 0 | silent bootstrap seeds rows/URLs and SHA |
| Saved state + one injected in-scope row | 1 | new snapshot/URL and SHA saved |
| Same SHA and initialized state | 0 | network/parser skipped |
| Changed SHA, same listings | 0 | SHA advances; identities remain known |
| Changed SHA, missing marker or zero rows | 0 | **old state and old SHA preserved** |
| Existing row removed | 0 | snapshot drops it; cumulative seen set retains URL |
| Removed row returns | snapshot: 1; cumulative: 0 | follows the documented identity model |
| Alert send fails | 0 successful | note: current code still advances watcher state |

The final row is a known high-recall risk: because state advances after a failed
Telegram send, that job is not retried on the next run. Any change touching send
or state ordering must include a mocked send-failure test and must not make this
failure mode worse; a deliberate retry/outbox redesign should add migration and
recovery tests.

For an affected cumulative source, inject **two rows with identical company,
role, location, and category but different full URLs** and require two alert
attempts. For a snapshot source, inject a row that changes only a volatile field
outside `(company, role, location, term)` and require zero alerts. Always include
a URL whose job ID is only in its query string.

## 5. Run every new commit, not only the final checkout

A later commit can hide a regression introduced earlier, and reviewers may test
or revert commits independently. The loop below checks out every commit after a
chosen base into a disposable worktree, runs that commit's workflow syntax and
production-parser regression, then removes the worktree.

First save the regression runner from section 3 somewhere outside the worktree
(it is already `/tmp/repo-watcher-regression.py`). Then run:

```sh
set -eu
BASE="${BASE:-origin/main}"
ROOT=$(git rev-parse --show-toplevel)
RUNNER=/tmp/repo-watcher-regression.py

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
    python3 -m json.tool .watcher_state.json >/dev/null
    python3 -m json.tool .bot_state.json >/dev/null
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
runner as part of that same commit (or use a runner versioned in the repository)
so the commit remains independently testable.

## 6. Upstream contract check for parser/scope changes

Only when `WATCHERS`, markers, or parsing changes, fetch upstream data read-only
at an exact commit SHA. Never run the workflow program itself. For each affected
watcher:

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

- `workflow_dispatch` is still the only trigger;
- bootstrap remains silent;
- empty parses cannot replace good state or advance the SHA;
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
