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
