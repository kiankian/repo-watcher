"""Unit tests for the pure parsing/dedup logic in watcher/core.py."""
import json

import pytest

from conftest import REPO_ROOT, load_fixture
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


def test_identity_is_total_and_injective_over_the_recorded_snapshots():
    """The property the no-miss guarantee rests on: every row gets exactly one identity, and no
    two rows in a run share one. A collision means an opening can never be reported as new."""
    for name in ("collapse_84.json", "collapse_28.json"):
        rows = load_fixture(name)
        paired = core.assign_identities(rows)

        assert len(paired) == len(rows), "total: one identity per row"
        assert len({identity for _, identity, _ in paired}) == len(rows), "injective: no collisions"


def test_identity_is_injective_over_the_live_state_snapshots():
    """Canary against the real state file rather than a fixture. Uniqueness must hold for any
    valid snapshot, so a failure here is a genuine signal, not fixture rot."""
    live = json.loads((REPO_ROOT / ".watcher_state.json").read_text())
    checked = 0
    for key, val in live.items():
        rows = val.get("rows")
        if not rows:
            continue
        paired = core.assign_identities(rows)
        assert len({i for _, i, _ in paired}) == len(rows), f"identity collision in {key}"
        checked += len(rows)
    if checked:
        print(f"checked {checked} live rows")


def test_occurrence_index_separates_otherwise_identical_rows():
    """Kudu Dynamics lists the same URL-less role three times; without the index they collapse
    to one identity and two openings become unreachable."""
    row = ["Kudu Dynamics", "Software Engineer Intern", "Chantilly, VA", "May 22", ""]
    paired = core.assign_identities([row, row, row])

    assert [i for _, i, _ in paired] == [
        "NOURL|Kudu Dynamics|Software Engineer Intern|Chantilly, VA|May 22#1",
        "NOURL|Kudu Dynamics|Software Engineer Intern|Chantilly, VA|May 22#2",
        "NOURL|Kudu Dynamics|Software Engineer Intern|Chantilly, VA|May 22#3",
    ]


def test_identity_prefers_the_url_over_the_row_text():
    with_url = ["Acme", "R", "L", "T", "https://acme.test/1"]
    without = ["Acme", "R", "L", "T", ""]

    assert core.row_identity(with_url, 1) == "https://acme.test/1|Acme|R|L|T#1"
    assert core.row_identity(without, 1) == "NOURL|Acme|R|L|T#1"


def test_identity_includes_the_term_so_a_relisted_req_is_new_again():
    """A requisition relisted for the next season keeps its URL. Without term in the identity it
    would be swallowed as already-seen."""
    summer = ["Acme", "SWE Intern", "NYC", "Summer 2026", "https://acme.test/job/1"]
    fall = ["Acme", "SWE Intern", "NYC", "Fall 2026", "https://acme.test/job/1"]

    assert core.row_identity(summer, 1) != core.row_identity(fall, 1)
    assert core.select_new([fall], seen={core.row_identity(summer, 1)})


# --- select_new -------------------------------------------------------------------------

def test_select_new_skips_seen_identities():
    rows = [["Acme", "R", "L", "T", "https://acme.test/1"],
            ["Beta", "R", "L", "T", "https://beta.test/1"]]
    seen = {core.row_identity(rows[0], 1)}

    fresh = core.select_new(rows, seen)

    assert [row[0] for row, _, _ in fresh] == ["Beta"]


def test_select_new_honours_legacy_urls():
    """Pre-migration state for the cumulative-URL sources holds bare URLs with no term, so those
    can only be matched on the URL -- the identity would not match and the job would re-alert."""
    row = ["Acme", "R", "L", "T", "https://acme.test/1"]

    assert core.select_new([row], seen=set()) , "unseen without legacy"
    assert core.select_new([row], seen=set(), legacy_urls={"https://acme.test/1"}) == []


def test_select_new_ignores_legacy_urls_for_rows_without_one():
    """An empty apply_url must never be matched against the legacy set, or every URL-less row
    would be suppressed at once."""
    row = ["Acme", "R", "L", "T", ""]

    assert core.select_new([row], seen=set(), legacy_urls={""}) != []


# --- migrate_state ----------------------------------------------------------------------

WATCHERS = [
    {"label": "Simplify Summer Repo", "state_key": "simplify#summer"},
    {"label": "Zapply Summer Repo", "state_key": "zapply#swe"},
]


def test_migrate_converts_a_rows_snapshot_to_identities():
    rows = [["Acme", "R", "L", "T", "https://acme.test/1"]]
    state = {"simplify#summer": {"last_sha": "abc", "rows": rows}}

    out = core.migrate_state(state, [], WATCHERS)["simplify#summer"]

    assert set(out) == {"last_sha", "seen", "seen_legacy_urls", "last_row_count", "outbox"}
    assert out["seen"] == ["https://acme.test/1|Acme|R|L|T#1"]
    assert out["outbox"] == [], "nothing was ever queued under the old shape"
    assert out["seen_legacy_urls"] == [], "rows carry a term, so no legacy fallback is needed"
    assert out["last_row_count"] == 1


def test_migrate_moves_bare_url_seen_lists_into_legacy():
    state = {"zapply#swe": {"last_sha": "abc", "seen": ["https://z.test/1", "https://z.test/2"]}}

    out = core.migrate_state(state, [], WATCHERS)["zapply#swe"]

    assert out["seen"] == [], "no identity can be reconstructed without a term"
    assert out["seen_legacy_urls"] == ["https://z.test/1", "https://z.test/2"]


def test_migrate_seeds_from_previously_sent_jobs():
    """Without this, anything alerted but not currently listed would alert again."""
    state = {"simplify#summer": {"last_sha": "abc", "rows": []}}
    records = [{"company": "Acme", "role": "R", "location": "L", "term": "T",
                "apply_url": "https://acme.test/1", "source": "Simplify Summer Repo"}]

    out = core.migrate_state(state, records, WATCHERS)["simplify#summer"]

    assert out["seen"] == ["https://acme.test/1|Acme|R|L|T#1"]


def test_migrate_attributes_records_from_a_renamed_label():
    """bot_state records keep whatever label was current when they were sent -- 'Simplify Repo'
    predates the rename to 'Simplify Summer Repo' -- so matching is on the label family."""
    state = {"simplify#summer": {"last_sha": "abc", "rows": []}}
    records = [{"company": "Acme", "role": "R", "location": "L", "term": "T",
                "apply_url": "https://acme.test/1", "source": "Simplify Repo"}]

    out = core.migrate_state(state, records, WATCHERS)["simplify#summer"]

    assert out["seen"] == ["https://acme.test/1|Acme|R|L|T#1"]


def test_migrate_numbers_repeated_url_less_records():
    """Three identical URL-less records must seed #1..#3, not three copies of #1."""
    state = {"simplify#summer": {"last_sha": "abc", "rows": []}}
    rec = {"company": "Kudu", "role": "R", "location": "L", "term": "T",
           "apply_url": "", "source": "Simplify Summer Repo"}

    out = core.migrate_state(state, [dict(rec), dict(rec), dict(rec)], WATCHERS)

    assert out["simplify#summer"]["seen"] == [
        "NOURL|Kudu|R|L|T#1", "NOURL|Kudu|R|L|T#2", "NOURL|Kudu|R|L|T#3",
    ]


def test_migrate_is_idempotent():
    state = {"simplify#summer": {"last_sha": "abc",
                                 "rows": [["Acme", "R", "L", "T", "https://acme.test/1"]]}}
    records = [{"company": "Beta", "role": "R", "location": "L", "term": "T",
                "apply_url": "https://beta.test/1", "source": "Simplify Summer Repo"}]

    once = core.migrate_state(state, records, WATCHERS)
    twice = core.migrate_state(once, records, WATCHERS)

    assert once == twice


def test_migrate_preserves_the_head_sha():
    """The sha must survive migration, or every source would re-fetch and re-diff on cutover."""
    state = {"simplify#summer": {"last_sha": "abc123", "rows": []}}

    assert core.migrate_state(state, [], WATCHERS)["simplify#summer"]["last_sha"] == "abc123"


def test_migrate_leaves_unrecognised_entries_alone():
    state = {"simplify#summer": {"last_sha": "a", "rows": []}, "_notes": "free text"}

    assert core.migrate_state(state, [], WATCHERS)["_notes"] == "free text"


def test_migrating_the_real_state_files_is_safe():
    """Runs against the committed state, so what it can assert depends on which side of the
    cutover that state is on.

    Legacy shape: this is the cutover rehearsal -- every row currently listed, and every job ever
    sent, must land in the seeded sets, or the first run after deploying re-alerts the board.

    Already migrated (the case since 2026-07-30): migration must be a no-op. A second pass that
    changed anything would mean every run re-seeds and the shape is unstable.

    Reading live state is deliberate -- it is the only check that runs against production data --
    but it does mean the assertions have to follow the file. An earlier version of this test read
    `seen` as bare URLs, which was true only before the cutover and broke the moment it happened.
    """
    state = json.loads((REPO_ROOT / ".watcher_state.json").read_text())
    bot_state = json.loads((REPO_ROOT / ".bot_state.json").read_text())
    records = list(bot_state["pending"].values()) + list(bot_state["applied"].values())
    watchers = [
        {"label": "Simplify Off-Season Repo", "state_key": "SimplifyJobs/Summer2026-Internships"},
        {"label": "Simplify Summer Repo",
         "state_key": "SimplifyJobs/Summer2026-Internships#summer"},
        {"label": "Vansh Off-Season Repo", "state_key": "vanshb03/Summer2027-Internships"},
        {"label": "Vansh Summer Repo", "state_key": "vanshb03/Summer2027-Internships#summer"},
        {"label": "Zapply Summer Repo", "state_key": "zapplyjobs/Internships-2027#swe"},
        {"label": "Speedyapply Summer Repo",
         "state_key": "speedyapply/2027-SWE-College-Jobs#faang"},
        {"label": "Speedyapply Summer Repo",
         "state_key": "speedyapply/2027-SWE-College-Jobs#quant"},
        {"label": "Speedyapply Summer Repo",
         "state_key": "speedyapply/2027-SWE-College-Jobs#other"},
    ]

    migrated = core.migrate_state(state, records, watchers)
    already_migrated = all(
        "seen_legacy_urls" in v for v in state.values() if isinstance(v, dict)
    )

    for key, val in state.items():
        out = migrated[key]
        assert set(out) == {
            "last_sha", "seen", "seen_legacy_urls", "last_row_count", "outbox",
        }
        if already_migrated:
            assert out == val, f"{key}: migration must not touch already-migrated state"
            continue
        # Nothing currently listed may be treated as new on the next run.
        rows = val.get("rows")
        if rows:
            assert core.select_new(
                rows, set(out["seen"]), out["seen_legacy_urls"]
            ) == [], f"{key} would re-alert its own rows"
        # Nor may any URL the old cumulative-URL sources had already seen. Pre-cutover only:
        # after it, `seen` holds identities rather than bare URLs.
        for url in val.get("seen") or []:
            assert core.select_new(
                [["C", "R", "L", "T", url]], set(out["seen"]), out["seen_legacy_urls"]
            ) == [], f"{key} would re-alert {url}"

    assert core.migrate_state(migrated, records, watchers) == migrated, "idempotent either way"


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


# --- job_hash occurrence suffix ---------------------------------------------------------

def test_job_hash_is_unchanged_at_occurrence_one():
    """The default must stay byte-identical to the original formula, or every Applied button
    already sitting in the chat is orphaned. test_job_hash_matches_the_hashes_in_bot_state is the
    real guard; this states the intent directly."""
    entry = ["Acme", "R", "L", "T", "https://acme.test/1"]

    assert core.job_hash(entry, "Label") == core.job_hash(entry, "Label", 1)
    assert core.job_hash(entry, "Label", occurrence=1) == core.job_hash(entry, "Label")


def test_job_hash_differs_per_occurrence():
    """Repeated identical rows must land on separate pending keys. Sharing one means each send
    overwrites the last, so tapping an earlier alert logs a sibling and the rest never can."""
    entry = ["Kudu Dynamics", "Software Engineer Intern", "Chantilly, VA", "May 22", ""]

    hashes = {core.job_hash(entry, "Vansh Summer Repo", occ) for occ in (1, 2, 3)}

    assert len(hashes) == 3


def test_every_row_in_a_run_gets_a_distinct_job_hash():
    """The live Vansh snapshot had 147 distinct identities but only 144 distinct hashes, so three
    delivered alerts were untrackable. Identity uniqueness alone is not enough — the hash has to
    be unique too, because that is what the button carries."""
    rows = [["Kudu Dynamics", "Software Engineer Intern", "Chantilly, VA", "May 22", ""]] * 3
    rows += [["General Dynamics", "Software Engineer Intern", "Pittsburgh, PA", "Apr 20", ""]] * 2

    triples = core.assign_identities(rows)
    hashes = {core.job_hash(row, "Vansh Summer Repo", occ) for row, _ident, occ in triples}

    assert len(hashes) == len(rows) == 5


def test_assign_identities_returns_the_occurrence():
    rows = [["Acme", "R", "L", "T", ""], ["Acme", "R", "L", "T", ""]]

    assert [occ for _row, _ident, occ in core.assign_identities(rows)] == [1, 2]


def test_select_new_carries_the_occurrence_through():
    """deliver_listings needs it to build the hash, so it must survive the filter."""
    rows = [["Acme", "R", "L", "T", ""], ["Acme", "R", "L", "T", ""]]

    fresh = core.select_new(rows, seen=set())

    assert [occ for _row, _ident, occ in fresh] == [1, 2]


# --- identity must not collapse on either half alone ------------------------------------

def test_a_replacement_sharing_a_generic_url_is_not_suppressed():
    """Two openings behind one generic link (a bare careers page, a Greenhouse embed). When one
    is replaced and the row count stays the same, a URL-only identity hands the replacement an
    occurrence that is already seen and it is silently missed."""
    a = ["Acme", "Backend Intern", "NYC", "T", "https://jobs.test/careers"]
    b = ["Acme", "Frontend Intern", "NYC", "T", "https://jobs.test/careers"]
    seen = {ident for _row, ident, _occ in core.assign_identities([a, b])}

    replacement = ["Acme", "Data Intern", "NYC", "T", "https://jobs.test/careers"]
    fresh = core.select_new([a, replacement], seen)

    assert [row[1] for row, _ident, _occ in fresh] == ["Data Intern"]


def test_openings_differing_only_by_url_still_stay_apart():
    """The other half of the same property: text alone would collapse Copart's Dallas reqs."""
    reqs = [
        ["Copart", "Software Engineer Intern", "Dallas, TX", "Summer 2026",
         f"https://copart.test/job/JR{n}"]
        for n in (101510, 109672, 109393, 109441)
    ]

    idents = {ident for _row, ident, _occ in core.assign_identities(reqs)}

    assert len(idents) == 4
    assert all(occ == 1 for _row, _ident, occ in core.assign_identities(reqs)), (
        "distinct URLs mean these are not occurrences of one stem"
    )


def test_url_less_rows_are_still_told_apart_by_their_text():
    a = ["Acme", "Backend Intern", "NYC", "T", ""]
    b = ["Acme", "Frontend Intern", "NYC", "T", ""]

    assert core.row_identity(a, 1) != core.row_identity(b, 1)
