"""Pure parsing and dedup logic for the repo-watcher workflow.

Everything here is side-effect free so it can be unit tested. All network, Telegram, git and
filesystem work stays in .github/workflows/watch-files.yml, which imports this module.

A "row" (also called an entry) is a 5-element list:

    [company, role, location, term, apply_url]

apply_url may be an empty string -- roughly a fifth of the rows on the Vansh boards have no
parseable link -- so nothing here may assume it is present.
"""
import hashlib
import re

CONTINUATION = "↳"  # the upstream boards use this to mean "same company as the row above"


def job_hash(entry, source, occurrence=1):
    """Stable id for a listing, embedded in the Telegram button as `apply:<hash>`.

    process_applies resolves a tapped button by looking this hash up in bot_state["pending"], so
    hundreds of already-sent messages in the chat depend on the exact hashed string. At
    occurrence 1 it is therefore byte-for-byte what it has always been.

    The occurrence suffix exists because repeated identical rows (Kudu Dynamics lists the same
    URL-less role three times) otherwise collide: every copy would write the same pending key,
    each send overwriting the last, so tapping an earlier alert would edit and log a sibling's
    message and the rest could never be recorded at all. Only occurrence > 1 changes shape, and
    no stored entry can be at occurrence > 1 — that collision is precisely what overwrote it —
    so nothing already issued is invalidated.
    """
    key_str = f"{source}|{entry[0]}|{entry[1]}|{entry[2]}|{entry[3]}|{entry[4]}"
    if occurrence > 1:
        key_str = f"{key_str}|#{occurrence}"
    return hashlib.sha256(key_str.encode()).hexdigest()[:12]


def strip_html(html):
    html = re.sub(r'<\s*/?\s*br\s*/?\s*>', ' ', html, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', html)
    return text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').strip()


def extract_apply_url(raw):
    m = re.search(r'href=["\']([^"\']+)["\']', raw)
    if m:
        return m.group(1)
    m = re.search(r'\]\(([^)\s]+)', raw)
    if m:
        return m.group(1)
    return ""


def extract_section(text, start_marker, end_marker):
    start_idx = text.find(start_marker)
    if start_idx == -1:
        return ""
    end_idx = text.find(end_marker, start_idx + len(start_marker))
    if end_idx == -1:
        end_idx = len(text)
    return text[start_idx:end_idx]


def parse_html_rows(section_text, term_col, apply_col, default_term):
    rows = []
    last_company = None
    min_cells = max(apply_col, term_col or 0) + 1
    for tr_match in re.finditer(r'<tr>\s*(.*?)\s*</tr>', section_text, re.DOTALL):
        cells = re.findall(r'<td>(.*?)</td>', tr_match.group(1), re.DOTALL)
        if len(cells) < min_cells:
            continue
        company = strip_html(cells[0])
        if company == CONTINUATION:
            company = last_company or CONTINUATION
        else:
            last_company = company
        term = strip_html(cells[term_col]) if term_col is not None else default_term
        rows.append([
            company,
            strip_html(cells[1]),
            strip_html(cells[2]),
            term,
            extract_apply_url(cells[apply_col]),
        ])
    return rows


def parse_markdown_rows(section_text, role_col=1, loc_col=2, apply_col=3,
                        term_col=4, default_term=None, min_cells=5, strip_bold=False):
    # Defaults reproduce the original Vansh behavior byte-for-byte. Zapply overrides
    # apply_col=5 / term_col=None / min_cells=6 / strip_bold=True for its 6-col table
    # whose company cells are bold (**Name**) and whose Posted/Visa cols are unusable.
    rows = []
    last_company = None
    for line in section_text.splitlines():
        body = line.strip()
        if not body.startswith('|'):
            continue
        parts = [c.strip() for c in body.strip('|').split('|')]
        if len(parts) < min_cells:
            continue
        if all(re.fullmatch(r'[-:\s]+', c) for c in parts if c):
            continue
        if parts[0].lower().replace('*', '') == 'company':
            continue
        company = strip_html(parts[0])
        if strip_bold:
            company = company.replace('**', '').strip()
        if company == CONTINUATION:
            company = last_company or CONTINUATION
        else:
            last_company = company
        term = default_term if term_col is None else strip_html(parts[term_col])
        rows.append([
            company,
            strip_html(parts[role_col]),
            strip_html(parts[loc_col]),
            term,
            extract_apply_url(parts[apply_col]),
        ])
    return rows


def fmt(entry):
    company, role, location, term, apply_url = entry[0], entry[1], entry[2], entry[3], entry[4]
    base = f"{company} — {role} | {location} | {term}"
    # A dead link is rendered as no link at all, exactly like the ~20% of Vansh rows that have
    # none. Printing "#" under a listing reads as something to tap and is worse than an honest
    # omission -- the company, role, location and term are still enough to go find the posting.
    # Only the message text changes: the identity and job_hash both keep the raw URL, so nothing
    # already sent is invalidated and no row is re-keyed by this.
    if apply_url and not is_placeholder_url(apply_url):
        return f"{base}\n  {apply_url}"
    return base


def row_key(r):
    """Legacy snapshot-diff key. Collapses distinct openings that differ only by apply URL
    (Copart lists several identical-looking Dallas reqs), which is why row_identity exists."""
    return (r[0], r[1], r[2], r[3])


# --- Identities -------------------------------------------------------------------------
#
# A listing's identity is what "have I already sent this?" is decided on. It must be total
# (every row gets one) and injective (no two rows in a run share one), or a row can be
# collapsed away and never alerted.
#
#   identity = (apply_url or NOURL) |company|role|location|term #occurrence
#
# Every field participates, because either half alone can collapse two distinct openings:
#
#   * URL alone is not enough. Boards sometimes publish a generic link shared by several rows
#     (a bare careers page, a Greenhouse embed). If two openings share one URL and one of them
#     is replaced while the number of rows sharing it stays the same, occurrence numbering
#     hands the replacement an identity that is already seen and it is silently missed.
#   * The row text alone is not enough. Copart posts several Dallas SWE-intern reqs that differ
#     only by Workday id; keyed on text they collapse and all but one are never alerted.
#
# Including the text costs a duplicate whenever upstream edits a role or location string
# in place. Measured over 2060 state snapshots spanning 18 days and 475 distinct (source, URL)
# pairs, that happened zero times -- so the cost is nil and the protection is free. term is in
# for the same reason, and it also lets a requisition relisted for a new season through.
#
# The occurrence index disambiguates rows identical in every field, which does happen: Kudu
# Dynamics lists the same URL-less role three times.

# Identities retained per source. This is a backstop, not a working size: the union grows by
# roughly a thousand a year on the busiest source, and the state file is committed on most runs,
# so it should not be set so high that a runaway parse can bloat the repo. Eviction drops the
# oldest identities, which can only cause a duplicate alert, never a missed one, and it is
# logged when it happens.
SEEN_CAP = 5000

# An upstream generator that stops emitting apply URLs re-keys every row in one commit: the
# identity is URL-first, so a table whose links all become "#" yields a full set of identities no
# source has ever seen, and every listing on the board looks like a discovery. Nothing about an
# individual row gives this away — each one is genuinely new *as an identity*.
#
# What gives it away is how much of the table the source still *recognizes*. Listings arrive one
# or a few at a time against a backdrop of rows already in `seen`, so even a large legitimate
# influx leaves most of the table familiar. A re-key rewrites the identity of every row at once,
# so recognition collapses to almost nothing — the source is looking at a board it has been
# watching for weeks and knows none of it.
#
# Note what this deliberately does *not* use: the row count. An earlier version of this guard
# subtracted the table's growth since the last parse, on the theory that real listings come with
# a row count that rose to match. That is true, but it credits a table *recovering* from a
# truncated parse as new capacity. A source that shrank to 20 rows and came back to 100 in the
# same update that re-keyed it computes 100 - 80 = 20 unexplained, slips under the floor, and
# sends the whole board — and those two failures are correlated rather than independent, since an
# upstream generator misfiring badly enough to truncate a read can blank a column in the same
# breath. Recognition has no such blind spot: it never asks how many rows there used to be.
#
# The trade is a false positive on an extreme legitimate expansion — a source growing 10x in one
# run has little of its new table in `seen` either. That direction is the safe one: the breaker
# holds delivery and says so, rather than flooding the chat, and the runbook covers clearing it.
#
# Bounds, against the 253 runs recorded before the 2026-08-01 Zapply incident. Recognition on a
# healthy run sits at 96-99% (Vansh discovering 6 of 157, Zapply 1 of 100); the worst legitimate
# case on record is the 84-row -> 28-row collapse, which still recognized 5 of 28 (18%) while
# alerting 23 genuinely-new rows. The incident recognized 0 of 100. The floor sits below that
# collapse and far above a re-key, and the absolute minimum keeps a small or mostly-empty table
# from tripping on a handful of rows.
IDENTITY_RESET_MIN = 25  # discoveries required before recognition is considered at all
IDENTITY_RESET_RECOGNITION = 0.10  # share of the parsed table that must still be familiar

# The breaker above catches a re-key by its shape, whatever caused it. This catches the specific
# cause seen on 2026-08-01 directly: an upstream generator that still emits the link markup but
# fills in a dead target, so `extract_apply_url` returns "#" and the row parses "successfully"
# with nothing to apply to. Such a row is not worth a message — the alert would carry a "#" where
# the link belongs — and it is the one failure a count-based guard cannot see, because the rows
# trickle onto a churning board a few at a time, far under any sane discovery floor. 19 of the
# 119 junk alerts in the incident arrived that way, after the initial flood had drained.
#
# An *absent* link is not degraded: roughly a fifth of the rows on the Vansh boards have no
# parseable URL at all, and those alerts are still useful — company, role, location and term are
# all there. Only a link that exists and goes nowhere counts.


def is_placeholder_url(url):
    """True for an apply link that is present but goes nowhere.

    A bare fragment ("#", "#apply") never addresses an ATS posting, and neither does a
    javascript: stub. Both mean the generator emitted a link it had no target for.
    """
    if not url:
        return False
    stripped = url.strip().lower()
    return stripped.startswith("#") or stripped.startswith("javascript:")


def is_identity_reset(new_count, row_count):
    """True when a run's discoveries look like the table re-keying, not new listings.

    new_count is what the source does not already know — neither recorded in `seen` nor already
    queued for delivery — so row_count - new_count is how much of this table it still recognizes.
    """
    if row_count <= 0 or new_count < IDENTITY_RESET_MIN:
        return False
    recognized = row_count - new_count
    return recognized < row_count * IDENTITY_RESET_RECOGNITION


def identity_stem(entry):
    # URL first so the stem stays greppable in state and logs, then every text field.
    base = entry[4] or "NOURL"
    return f"{base}|{entry[0]}|{entry[1]}|{entry[2]}|{entry[3]}"


def row_identity(entry, occurrence):
    return f"{identity_stem(entry)}#{occurrence}"


def assign_identities(rows):
    """Return (row, identity, occurrence) triples, numbering repeats of a stem from 1.

    The occurrence is returned rather than left to be parsed back out of the identity string,
    because job_hash needs it too and re-deriving it from a trailing "#N" would break on any
    stem that legitimately ends that way.
    """
    counts = {}
    triples = []
    for row in rows:
        stem = identity_stem(row)
        counts[stem] = counts.get(stem, 0) + 1
        triples.append((row, f"{stem}#{counts[stem]}", counts[stem]))
    return triples


def select_new(rows, seen, legacy_urls=()):
    """Rows never delivered before, as (row, identity, occurrence) triples.

    seen holds identities. legacy_urls holds bare apply URLs recorded before identities
    existed -- those predate the term/occurrence suffix, so they can only be matched on URL.
    """
    legacy = set(legacy_urls)
    fresh = []
    for row, identity, occurrence in assign_identities(rows):
        if identity in seen:
            continue
        if row[4] and row[4] in legacy:
            continue
        fresh.append((row, identity, occurrence))
    return fresh


def _label_family(label):
    """'Simplify Summer Repo' -> 'simplify'.

    Watcher labels get renamed ('Simplify Repo' became 'Simplify Summer Repo') while records in
    bot_state keep whatever label was current when they were sent. Matching on the family keeps
    those older records attached to the sources that could have produced them.
    """
    return (label or "").strip().split(" ")[0].lower()


def migrate_state(state, bot_records, watchers, cap=SEEN_CAP):
    """Convert legacy per-source state into the unified identity shape.

    Legacy shapes:
        {last_sha, rows: [...]}  snapshot-diff sources
        {last_sha, seen: [url]}  cumulative-URL sources (bare URLs, no term recorded)

    Unified shape:
        {last_sha, seen: [identity], seen_legacy_urls: [url], last_row_count: int}

    Seeded from the currently-listed rows *and* everything in bot_state, so the first run after
    the change alerts nothing. Idempotent: a source already carrying seen_legacy_urls is
    returned untouched.
    """
    families = {}
    for w in watchers:
        families.setdefault(_label_family(w["label"]), set()).add(w["state_key"])

    # Everything ever delivered, attributed to the sources that could have sent it.
    delivered = {}
    counters = {}
    for rec in bot_records:
        entry = [rec.get("company", ""), rec.get("role", ""), rec.get("location", ""),
                 rec.get("term", ""), rec.get("apply_url", "")]
        stem = identity_stem(entry)
        for key in families.get(_label_family(rec.get("source")), ()):
            idents, urls = delivered.setdefault(key, (set(), set()))
            counters[(key, stem)] = counters.get((key, stem), 0) + 1
            idents.add(f"{stem}#{counters[(key, stem)]}")
            if entry[4]:
                urls.add(entry[4])

    migrated = {}
    for key, val in state.items():
        if not isinstance(val, dict) or "last_sha" not in val:
            migrated[key] = val  # not a source entry; leave alone
            continue
        if "seen_legacy_urls" in val:
            migrated[key] = val  # already migrated
            continue

        rows = val.get("rows")
        seen, legacy = [], set()
        if rows is not None:
            # Rows carry a term, so their identities reconstruct exactly.
            seen = [identity for _, identity, _occ in assign_identities(rows)]
        elif val.get("seen") is not None:
            # The old cumulative-URL sources stored bare URLs with no term recorded, so these
            # can only ever be matched on URL. This is the only reason seen_legacy_urls exists,
            # and keeping it that narrow matters: a legacy URL also suppresses the same
            # requisition relisted under a new term, which an identity would let through.
            legacy |= set(val["seen"])

        extra_idents, _extra_urls = delivered.get(key, (set(), set()))
        already = set(seen)
        seen += sorted(i for i in extra_idents if i not in already)

        migrated[key] = {
            "last_sha": val["last_sha"],
            "seen": seen[-cap:],
            "seen_legacy_urls": sorted(legacy),
            "last_row_count": len(rows) if rows is not None else 0,
            # Nothing was ever queued under the old shape, so migration starts it empty.
            "outbox": [],
        }
    return migrated
