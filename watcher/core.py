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


def job_hash(entry, source):
    """Stable id for a listing, embedded in the Telegram button as `apply:<hash>`.

    Do not change the inputs or the truncation: process_applies resolves a tapped button by
    looking this hash up in bot_state["pending"], and hundreds of already-sent messages in the
    chat carry hashes computed by the current formula.
    """
    key_str = f"{source}|{entry[0]}|{entry[1]}|{entry[2]}|{entry[3]}|{entry[4]}"
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
    if apply_url:
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
#   identity = (apply_url or ROWKEY:company|role|location) + "|" + term + "#" + occurrence
#
# apply_url first, because it distinguishes openings that are textually identical -- Copart
# posts several Dallas SWE-intern reqs differing only by Workday id, and row_key merges them.
# The ROWKEY fallback covers rows with no parseable link (~a fifth of the Vansh rows). term is
# included because it is free: across every (watcher, url) pair on record, one URL never
# carried two different terms, and including it means a req relisted for a new season is
# treated as new. The occurrence index disambiguates rows that are identical even down to a
# missing URL (Kudu Dynamics lists the same URL-less role three times).

# Identities retained per source. This is a backstop, not a working size: the union grows by
# roughly a thousand a year on the busiest source, and the state file is committed on most runs,
# so it should not be set so high that a runaway parse can bloat the repo. Eviction drops the
# oldest identities, which can only cause a duplicate alert, never a missed one, and it is
# logged when it happens.
SEEN_CAP = 5000


def identity_stem(entry):
    base = entry[4] or f"ROWKEY:{entry[0]}|{entry[1]}|{entry[2]}"
    return f"{base}|{entry[3]}"


def row_identity(entry, occurrence):
    return f"{identity_stem(entry)}#{occurrence}"


def assign_identities(rows):
    """Pair each row with its identity, numbering repeats of the same stem from 1."""
    counts = {}
    paired = []
    for row in rows:
        stem = identity_stem(row)
        counts[stem] = counts.get(stem, 0) + 1
        paired.append((row, f"{stem}#{counts[stem]}"))
    return paired


def select_new(rows, seen, legacy_urls=()):
    """Rows never delivered before, as (row, identity) pairs.

    seen holds identities. legacy_urls holds bare apply URLs recorded before identities
    existed -- those predate the term/occurrence suffix, so they can only be matched on URL.
    """
    legacy = set(legacy_urls)
    fresh = []
    for row, identity in assign_identities(rows):
        if identity in seen:
            continue
        if row[4] and row[4] in legacy:
            continue
        fresh.append((row, identity))
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
            seen = [identity for _, identity in assign_identities(rows)]
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
        }
    return migrated
