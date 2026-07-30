"""Unit tests for the pure parsing/dedup logic in watcher/core.py."""
import json

import pytest

from conftest import load_fixture
from watcher import core


# --- job_hash: frozen, because live Telegram buttons carry these values ------------------

def test_job_hash_matches_the_hashes_in_bot_state():
    """Every already-sent message in the chat has `apply:<hash>` baked into its button, and
    process_applies resolves the tap via bot_state["pending"][hash]. If this test fails, every
    outstanding Applied button in the chat has been orphaned."""
    bot_state = load_fixture("bot_state.json")

    for expected_hash, job in bot_state["pending"].items():
        entry = [job["company"], job["role"], job["location"], job["term"], job["apply_url"]]
        assert core.job_hash(entry, job["source"]) == expected_hash


# --- strip_html / extract_apply_url -----------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("<b>Acme</b>", "Acme"),
    ("Boston<br>Seattle", "Boston Seattle"),
    ("Boston<BR/>Seattle", "Boston Seattle"),
    ("A &amp; B", "A & B"),
    ("&lt;script&gt;", "<script>"),
    ("  padded  ", "padded"),
])
def test_strip_html(raw, expected):
    assert core.strip_html(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ('<a href="https://x.test/job">Apply</a>', "https://x.test/job"),
    ("<a href='https://x.test/j2'>Apply</a>", "https://x.test/j2"),
    ("[Apply](https://x.test/j3)", "https://x.test/j3"),
    ("no link here", ""),
])
def test_extract_apply_url(raw, expected):
    assert core.extract_apply_url(raw) == expected


def test_extract_apply_url_keeps_the_query_string():
    """Some ATS job ids live only in the query string (Greenhouse ?gh_jid=), and the dedup
    identity is derived from the URL, so it must not be truncated."""
    url = "https://boards.greenhouse.io/embed/job_app?for=gemini&token=7875125"
    assert core.extract_apply_url(f'<a href="{url}">Apply</a>') == url


# --- extract_section --------------------------------------------------------------------

def test_extract_section_bounds():
    text = "intro\n## START\nbody\n## END\ntail"
    assert core.extract_section(text, "## START", "## END") == "## START\nbody\n"


def test_extract_section_runs_to_eof_when_the_end_marker_is_missing():
    text = "intro\n## START\nbody"
    assert core.extract_section(text, "## START", "## MISSING") == "## START\nbody"


def test_extract_section_returns_empty_when_the_start_marker_is_missing():
    """This is how a renamed upstream heading surfaces: an empty section, which the workflow's
    zero-row guard must then refuse to write to state."""
    assert core.extract_section("intro\nbody", "## GONE", "## END") == ""


# --- parse_html_rows --------------------------------------------------------------------

HTML_HEAD = "## S\n<table><tr><th>Company</th><th>Role</th><th>Loc</th><th>Apply</th></tr>"


def _html(*trs):
    return HTML_HEAD + "".join(trs) + "</table>\n## E\n"


def test_parse_html_rows_basic():
    rows = core.parse_html_rows(
        _html('<tr><td>Acme</td><td>SWE Intern</td><td>NYC</td>'
              '<td><a href="https://x.test/a">Apply</a></td></tr>'),
        term_col=None, apply_col=3, default_term="Summer 2026",
    )
    assert rows == [["Acme", "SWE Intern", "NYC", "Summer 2026", "https://x.test/a"]]


def test_parse_html_rows_skips_a_th_header():
    rows = core.parse_html_rows(_html(), term_col=None, apply_col=3, default_term="T")
    assert rows == []


def test_parse_html_rows_expands_the_continuation_marker():
    """Upstream writes ↳ for "same company as the row above"; carrying the name forward keeps
    the identity stable instead of producing a row literally named ↳."""
    rows = core.parse_html_rows(
        _html('<tr><td>Acme</td><td>Role A</td><td>NYC</td><td><a href="https://x/1">x</a></td></tr>',
              '<tr><td>↳</td><td>Role B</td><td>SF</td><td><a href="https://x/2">x</a></td></tr>'),
        term_col=None, apply_col=3, default_term="T",
    )
    assert [r[0] for r in rows] == ["Acme", "Acme"]


def test_parse_html_rows_reads_a_real_term_column():
    rows = core.parse_html_rows(
        "## S<table><tr><td>Acme</td><td>Role</td><td>NYC</td><td>Fall 2026</td>"
        '<td><a href="https://x/1">x</a></td></tr></table>',
        term_col=3, apply_col=4, default_term="unused",
    )
    assert rows[0][3] == "Fall 2026"


def test_parse_html_rows_keeps_rows_without_a_link():
    """A row with no parseable URL still has to become an alert -- roughly a fifth of the Vansh
    rows are like this -- so the parser must keep it with an empty apply_url."""
    rows = core.parse_html_rows(
        _html("<tr><td>Acme</td><td>Role</td><td>NYC</td><td>closed</td></tr>"),
        term_col=None, apply_col=3, default_term="T",
    )
    assert rows == [["Acme", "Role", "NYC", "T", ""]]


# --- parse_markdown_rows ----------------------------------------------------------------

def test_parse_markdown_rows_vansh_defaults():
    md = (
        "| Company | Role | Location | Apply | Age |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| Acme | SWE Intern | NYC | [Apply](https://x.test/a) | Jul 24 |\n"
    )
    assert core.parse_markdown_rows(md) == [
        ["Acme", "SWE Intern", "NYC", "Jul 24", "https://x.test/a"]
    ]


def test_parse_markdown_rows_skips_header_and_separator():
    md = (
        "| Company | Role | Location | Apply | Age |\n"
        "| :--- | ---: | --- | --- | --- |\n"
    )
    assert core.parse_markdown_rows(md) == []


def test_parse_markdown_rows_zapply_overrides():
    """Zapply's table is 6 columns with bold company cells and an unusable Posted column."""
    md = (
        "| **Acme** | SWE Intern | New York | Recently | Yes | [Apply](https://x.test/z) |\n"
    )
    rows = core.parse_markdown_rows(
        md, apply_col=5, term_col=None, default_term="Summer 2027",
        min_cells=6, strip_bold=True,
    )
    assert rows == [["Acme", "SWE Intern", "New York", "Summer 2027", "https://x.test/z"]]


def test_parse_markdown_rows_ignores_non_table_lines():
    md = "some prose\n\n| Acme | R | L | [a](https://x/1) | Jul 1 |\nmore prose\n"
    assert len(core.parse_markdown_rows(md)) == 1


# --- fmt --------------------------------------------------------------------------------

def test_fmt_includes_the_url_on_its_own_line():
    out = core.fmt(["Acme", "SWE Intern", "NYC", "Summer 2026", "https://x.test/a"])
    assert out.splitlines() == ["Acme — SWE Intern | NYC | Summer 2026", "  https://x.test/a"]


def test_fmt_omits_the_url_line_when_absent():
    out = core.fmt(["Acme", "SWE Intern", "NYC", "Summer 2026", ""])
    assert out == "Acme — SWE Intern | NYC | Summer 2026"


# --- row_key: documents the defect Phase B replaces --------------------------------------

def test_row_key_collapses_openings_that_differ_only_by_url():
    """Copart posts several Dallas SWE-intern reqs that differ only by Workday id. row_key
    cannot tell them apart, so the snapshot diff reports one 'new' listing for the whole
    cluster and the rest are never alerted."""
    reqs = [
        ["Copart", "Software Engineer Intern", "Dallas, TX", "Summer 2026",
         f"https://copart.test/JR{n}"]
        for n in (101510, 109672, 109393, 109441)
    ]
    assert len({core.row_key(r) for r in reqs}) == 1


def test_the_recorded_snapshot_collapse_drops_rows_that_were_already_alerted():
    """Replay of the real 2026-07-30 01:37 UTC run: the extracted snapshot fell from 84 rows to
    28, with only 5 keys in common. Under wholesale snapshot replacement the vanished rows are
    forgotten and re-alert whenever they come back -- the duplicate source Phase B removes."""
    before = load_fixture("collapse_84.json")
    after = load_fixture("collapse_28.json")

    keys_before = {core.row_key(r) for r in before}
    keys_after = {core.row_key(r) for r in after}

    assert len(before) == 84 and len(after) == 28
    assert len(keys_after - keys_before) == 23, "genuinely new listings that run"
    assert len(keys_before & keys_after) == 5, "barely any overlap -- not a normal delta"
    assert len(keys_before - keys_after) == 71, "distinct keys dropped from the snapshot"
    assert len([r for r in before if core.row_key(r) not in keys_after]) == 79, (
        "79 rows for 71 keys: 8 rows sit in same-key clusters"
    )


def test_the_collapsed_snapshot_hides_whole_clusters_of_openings():
    """The 84-row snapshot holds 84 rows but only 76 distinct row_keys. Those 8 surplus rows are
    invisible to the snapshot diff: once one member of a cluster is in state, the others can
    never be reported as new. Palo Alto Networks and Copart -- the two companies with the most
    repeat alerts on record -- are both in here."""
    before = load_fixture("collapse_84.json")

    assert len(before) == 84
    assert len({core.row_key(r) for r in before}) == 76

    clusters = {}
    for row in before:
        clusters.setdefault(core.row_key(row), []).append(row)
    multi = {k: v for k, v in clusters.items() if len(v) > 1}

    assert len(multi) == 6
    assert {k[0] for k in multi} == {
        "Palo Alto Networks", "Copart", "ACI Worldwide", "GE Vernova",
    }
    # Every member of a cluster is a genuinely different opening: distinct apply URLs.
    for rows in multi.values():
        assert len({r[4] for r in rows}) == len(rows)
