# Repo Watcher Parsing Reference

This document describes exactly what is being watched, how upstream file formats differ, and how commit diffs are parsed.

## Watched Repos

Four watchers, two repos × two files each:

| Label | Repo | Branch | File | Format Type | Section |
|---|---|---|---|---|---|
| Simplify Off-Season Repo | `SimplifyJobs/Summer2026-Internships` | `dev` | `README-Off-Season.md` | HTML table embedded in markdown | SWE active only |
| Simplify Summer Repo | `SimplifyJobs/Summer2026-Internships` | `dev` | `README.md` | HTML table embedded in markdown | SWE only (active + inactive) |
| Vansh Off-Season Repo | `vanshb03/Summer2027-Internships` | `dev` | `OFFSEASON_README.md` | Markdown pipe table rows | full `## The List` table |
| Vansh Summer Repo | `vanshb03/Summer2027-Internships` | `dev` | `README.md` | Markdown pipe table rows | full `## The List` table |

Source of truth for watcher config: `.github/workflows/watch-files.yml` in the `WATCHERS` list.

Section markers per watcher:

| Label | `section_start` | `section_end` |
|---|---|---|
| Simplify Off-Season Repo | `## 💻 Software Engineering Internship Roles` | `<summary>🗃️ Inactive roles` |
| Simplify Summer Repo | `## 💻 Software Engineering Internship Roles` | `## 📱 Product Management Internship Roles` |
| Vansh Off-Season Repo | `## The List` | `## We love our contributors` |
| Vansh Summer Repo | `## The List` | `## We love our contributors` |

The two Simplify watchers parse **only** the Software Engineering category. The Off-Season end marker (`<summary>🗃️ Inactive roles`) stops at the SWE inactive block, so only *active* SWE rows are captured; the Summer end marker is the next category heading, so its SWE *inactive* rows fall inside the slice too. Both Vansh watchers parse the entire uncategorized `## The List` table (all role types).

Column layout differs by file, so the HTML watchers carry per-watcher `term_col` / `apply_col` / `default_term`:

| Label | `term_col` | `apply_col` | `default_term` |
|---|---|---|---|
| Simplify Off-Season Repo | `3` | `4` | `""` |
| Simplify Summer Repo | `None` (uses default) | `3` | `"Summer 2026"` |

## State + Snapshot Comparison Flow

1. The workflow reads `.watcher_state.json`. Each repo entry stores `{last_sha, rows}` where `rows` is the parsed table snapshot at `last_sha`.
2. For each watched repo, it fetches the latest commit SHA:
   - `GET /repos/{owner}/{repo}/commits/{branch}`
3. If `last_sha == latest_sha` AND `rows` is already populated, skip — nothing to do.
4. Otherwise fetch the file content at `latest_sha`:
   - `GET https://raw.githubusercontent.com/{owner}/{repo}/{latest_sha}/{file}`
5. Slice the relevant section using `section_start`/`section_end` markers (Simplify: SWE active only; vanshb03: full table region).
6. Parse rows from the section into `[company, role, location, term_or_date, apply_url]` lists, resolving any `↳` Company cell to the previous row's company name.
7. Compare the new row set against `rows` from saved state by `(company, role, location, term_or_date)` key. Anything in the new set but not the previous is a new listing.
8. Bootstrap path: if `rows` is missing in state (first run after this code shipped), store the snapshot silently — no alert.
9. Update `.watcher_state.json` to `{last_sha: latest_sha, rows: curr_rows}`.

Why snapshot (not diff) parsing:
- Section filtering is trivial — slice by markers, ignore everything outside.
- `↳` ambiguity is resolved deterministically as we walk rows in order.
- Re-orderings, `↳`-flips between adjacent rows, and active↔inactive `🔒` flips are all silenced by construction (same key → same row).
- Cross-section moves into SWE (e.g. Data Science → SWE in Simplify) appear as a true new row in the SWE snapshot.
- Inactive→Active SWE re-listings naturally surface as new rows since the inactive `<details>` block is excluded by the section markers.

## Repo Format Details

## 1) Simplify Repos (`README-Off-Season.md` + `README.md`)

Both Simplify files contain 5 categorized sections (SWE / PM / Data Science / Quant Finance / Hardware Engineering), each with an active table followed by an Inactive `<details>` block. **Only the SWE section is watched** in each. Section bounds differ slightly between the two files:

- **Off-Season** (`README-Off-Season.md`): Start `## 💻 Software Engineering Internship Roles` → End `<summary>🗃️ Inactive roles` (first occurrence after start) — captures SWE *active* rows only.
- **Summer** (`README.md`): Start `## 💻 Software Engineering Internship Roles` → End `## 📱 Product Management Internship Roles` — captures SWE active *and* inactive rows (the inactive `<details>` block sits before the PM heading).

The two files also differ in column layout: Off-Season has a Terms column (`term_col=3`, `apply_col=4`); Summer has no Terms column, so the watcher uses `apply_col=3` and stamps a fixed `default_term="Summer 2026"`.

Inside the section, each row is an HTML `<tr>`:

```html
<tr>
<td><strong><a href="...">Company</a></strong></td>
<td>Role</td>
<td>Location</td>
<td>Term</td>
<td><div align="center"><a href="..."><img alt="Apply"></a> ...</div></td>
<td>Age</td>
</tr>
```

Parsing behavior:
- Slice the section between markers.
- `re.finditer(r'<tr>\s*(.*?)\s*</tr>', section, re.DOTALL)` to enumerate rows.
- `re.findall(r'<td>(.*?)</td>', tr, re.DOTALL)` for cells; require ≥5.
- Keep cells `[0..4]`: company, role, location, term, apply URL (Age column 5 is dropped — it changes every poll).
- Resolve `↳` in column 0 to the previous row's company name.

Normalization (via `strip_html`):
- `<br>` variants → space.
- Remaining tags stripped.
- `&amp;`, `&lt;`, `&gt;` decoded.

## 2) Vansh Repos (`OFFSEASON_README.md` + `README.md`)

Both Vansh files use the same format: one uncategorized pipe-table covering the whole listing. Same section bounds for both (chosen to skip legend + footer):

- Start: `## The List`
- End:   `## We love our contributors`

Because the table is uncategorized, **all role types** (SWE, quant, ML, etc.) flow through from both Vansh watchers — there is no SWE-only filter here, unlike Simplify.

Typical row:

```md
| Company | Role | Location | <a href="..."><img alt="Apply"></a> | Apr 22 |
```

Parsing behavior:
- Per line: strip; require start with `|`; split on `|`.
- Skip separator (`| --- | --- | ... |`) and the literal header (`Company` in column 0).
- Require ≥5 cells; keep `[0, 1, 2, 4]` (drop apply markup column for the URL extraction step) plus `extract_apply_url(parts[3])`.
- Resolve `↳` in column 0 to the previous row's company name.

## Listing Identity and Change Classification

Listing key:

```text
(company, role, location, term_or_date)
```

`apply_url` is intentionally not part of the key — companies sometimes rotate query strings.

Classification:
- Build `prev_keys` from saved `state[repo].rows`.
- For each row in `curr_rows`, alert iff its key is not in `prev_keys`.
- Removed (closures): silently dropped — we only alert on additions.
- Active↔Inactive toggles in Simplify never appear as alerts because the inactive section is outside our parsed range. Inactive→Active naturally surfaces as a "new" row.

## Practical Examples

Simplify SWE row inside the section:

```html
<tr>
<td><strong><a href="https://simplify.jobs/c/Example">Example Co</a></strong></td>
<td>Software Engineer Intern</td>
<td>San Francisco, CA</td>
<td>Spring 2027</td>
<td><div align="center"><a href="https://jobs.example.com/apply"><img alt="Apply"></a></div></td>
<td>0d</td>
</tr>
```
→ `["Example Co", "Software Engineer Intern", "San Francisco, CA", "Spring 2027", "https://jobs.example.com/apply"]`

Vansh row using `↳` continuation:

```md
| Verkada | AI Software Engineer Intern | San Mateo, CA | <a …> | Apr 24 |
| ↳       | Backend Software Engineer Intern | San Mateo, CA | <a …> | Apr 24 |
```
→ Both rows resolve company to `Verkada`.

## Known Parsing Constraints

- `raw.githubusercontent.com` returns the file at any SHA; one fetch per repo per change.
- If section markers ever drift in upstream (rename/emoji change), `extract_section` returns `""` and the parser yields zero rows. We log a warning and skip state update so we don't false-bootstrap.
- HTML structure drift (missing `</td>` close, nested `<tr>` inside `<details>`) can break extraction — none observed today but worth monitoring.
- Markdown rows containing unescaped `|` inside cell content can shift columns. Vansh's source has this risk but hasn't bitten.

## Where to Update If Upstream Formats Change

Update these in `.github/workflows/watch-files.yml`:
- `WATCHERS[*].section_start` / `section_end` if section headings change.
- `parse_html_rows` if the Simplify cell layout changes (column count, nesting).
- `parse_markdown_rows` if Vansh's column order changes.
- `strip_html` / `extract_apply_url` for HTML entity or link-markup drift.
- `row_key` if the identity tuple needs to change.

Also update this document.
