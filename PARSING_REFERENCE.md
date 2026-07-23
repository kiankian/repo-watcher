# Repo Watcher Parsing Reference

This document describes exactly what is being watched, how upstream file formats differ, and how commit diffs are parsed.

## Watched Repos

Eight watchers across four repos:

| Label | Repo | Branch | File | Format Type | Section |
|---|---|---|---|---|---|
| Simplify Off-Season Repo | `SimplifyJobs/Summer2026-Internships` | `dev` | `README-Off-Season.md` | HTML table embedded in markdown | SWE active only |
| Simplify Summer Repo | `SimplifyJobs/Summer2026-Internships` | `dev` | `README.md` | HTML table embedded in markdown | SWE only (active + inactive) |
| Vansh Off-Season Repo | `vanshb03/Summer2027-Internships` | `dev` | `OFFSEASON_README.md` | Markdown pipe table rows | full `## The List` table |
| Vansh Summer Repo | `vanshb03/Summer2027-Internships` | `dev` | `README.md` | Markdown pipe table rows | full `## The List` table |
| Zapply Summer Repo | `zapplyjobs/Internships-2027` | `main` | `README.md` | Markdown pipe table rows (6-col) | `💻 Software Engineering` table only |
| Speedyapply Summer Repo | `speedyapply/2027-SWE-College-Jobs` | `main` | `README.md` | Markdown pipe table rows (6-col) | USA Internships → `FAANG+` table |
| Speedyapply Summer Repo | `speedyapply/2027-SWE-College-Jobs` | `main` | `README.md` | Markdown pipe table rows (6-col) | USA Internships → `Quant` table |
| Speedyapply Summer Repo | `speedyapply/2027-SWE-College-Jobs` | `main` | `README.md` | Markdown pipe table rows (5-col) | USA Internships → `Other` table |

Source of truth for watcher config: `.github/workflows/watch-files.yml` in the `WATCHERS` list.

Section markers per watcher:

| Label | `section_start` | `section_end` |
|---|---|---|
| Simplify Off-Season Repo | `## 💻 Software Engineering Internship Roles` | `<summary>🗃️ Inactive roles` |
| Simplify Summer Repo | `## 💻 Software Engineering Internship Roles` | `## 📱 Product Management Internship Roles` |
| Vansh Off-Season Repo | `## The List` | `## We love our contributors` |
| Vansh Summer Repo | `## The List` | `## We love our contributors` |
| Zapply Summer Repo | `💻 <strong>Software Engineering</strong>` | `📊 <strong>Data Science` |
| Speedyapply Summer Repo (FAANG+) | `<!-- TABLE_FAANG_START -->` | `<!-- TABLE_FAANG_END -->` |
| Speedyapply Summer Repo (Quant) | `<!-- TABLE_QUANT_START -->` | `<!-- TABLE_QUANT_END -->` |
| Speedyapply Summer Repo (Other) | `<!-- TABLE_START -->` | `<!-- TABLE_END -->` |

The two Simplify watchers parse **only** the Software Engineering category. The Off-Season end marker (`<summary>🗃️ Inactive roles`) stops at the SWE inactive block, so only *active* SWE rows are captured; the Summer end marker is the next category heading, so its SWE *inactive* rows fall inside the slice too. Both Vansh watchers parse the entire uncategorized `## The List` table (all role types). The Zapply watcher slices only the first (`💻 Software Engineering`) of six `<details>` category tables; its `section_end` is a **substring** of the next section's summary (`📊 <strong>Data Science`), deliberately stopping before the literal `&` in "Data Science & AI" to avoid `&`-vs-`&amp;` raw-byte ambiguity. The three Speedyapply watchers slice on **HTML-comment delimiters** the generator emits around each table (`<!-- TABLE_FAANG_START/END -->`, `<!-- TABLE_QUANT_START/END -->`, `<!-- TABLE_START/END -->`) — machine markers, so there is no emoji/heading ambiguity. Note `<!-- TABLE_START -->` (the "Other" table) is **not** a substring of the FAANG/Quant markers, so `str.find` slices it correctly.

Column layout differs by file, so the HTML watchers **and** the Zapply markdown watcher carry per-watcher column config:

| Label | `term_col` | `apply_col` | `default_term` |
|---|---|---|---|
| Simplify Off-Season Repo | `3` | `4` | `""` |
| Simplify Summer Repo | `None` (uses default) | `3` | `"Summer 2026"` |
| Zapply Summer Repo | `None` (uses default) | `5` | `"Summer 2027"` |
| Speedyapply Summer Repo — FAANG+ | `None` (uses default) | `4` | `"FAANG+"` |
| Speedyapply Summer Repo — Quant | `None` (uses default) | `4` | `"Quant"` |
| Speedyapply Summer Repo — Other | `None` (uses default) | `3` | `"Other"` |

Zapply additionally carries `role_col=1`, `loc_col=2`, `min_cells=6`, `strip_bold=True`, and `dedup="cumulative_url"` (see below). Speedyapply carries `role_col=1`, `loc_col=2`, `dedup="cumulative_url"`, and per-table `apply_col`/`min_cells` (FAANG+/Quant are 6-col → `apply_col=4`, `min_cells=6`; Other drops the Salary column → `apply_col=3`, `min_cells=5`); it needs **no** `strip_bold` because its company cells are HTML `<strong>` (already removed by `strip_html`), not markdown `**`. These all feed the now-column-configurable `parse_markdown_rows` (its defaults reproduce the Vansh layout, so the Vansh call is unchanged).

## State + Snapshot Comparison Flow

1. The workflow reads `.watcher_state.json`. Most repo entries store `{last_sha, rows}` where `rows` is the parsed table snapshot at `last_sha`. **Zapply and Speedyapply are the exceptions** — their entries store `{last_sha, seen}` (see "Exception: cumulative-URL dedup" below).
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

### Exception: cumulative-URL dedup (Zapply + Speedyapply)

Zapply does **not** use the snapshot-diff flow above (config `"dedup": "cumulative_url"`). Its `💻 Software Engineering` table is regenerated and re-sorted every ~15 min and is capped at ~100 visible rows (bot `z-apply`, commit message `auto: regenerate README from pipeline (centralized)`), so listings near the boundary flap in and out of the window. A snapshot diff would re-alert every time a dropped job re-enters. Its Role/Location cells are also truncated with a literal `...` and its Posted column is the constant `Recently`, so `(company, role, location, term)` is a colliding, unstable key.

Instead the Zapply watcher:

1. Fetches the latest SHA; skips when `last_sha == latest_sha` and `seen` is already populated.
2. Slices the `💻 Software Engineering` section and parses it with the shared `parse_markdown_rows` (6-col config).
3. **Unconditional empty-guard:** if 0 rows parse (marker/format drift), it logs and skips — never seeding an empty set or advancing the SHA.
4. Keys each job by its **apply URL** (verified unique per snapshot — the only stable field).
5. **Silent bootstrap:** on first run (no `seen` yet), seeds the current URLs and alerts nothing.
6. Otherwise alerts each URL **not** already in `seen`, then appends the new URLs to `seen`.
7. Stores `{last_sha, seen}` where `seen` is a cumulative, append-only list capped at `URL_CAP` (3000) most-recent URLs. Because the fresh window is <1 week / ~100 rows, a pruned URL is weeks-dead and cannot re-appear, so the cap never causes a re-alert.

Net effect: a URL already seen never re-alerts, so churn (drop-out then re-add) is silent; only genuinely-new SWE roles alert.

**Speedyapply** uses the identical mechanism for all three of its USA-Internships tables (FAANG+, Quant, Other), for a different reason. Its Role/Location are full-text (not truncated), but many genuinely-distinct openings share the same `(company, role, location)` and differ only by apply URL — e.g. Copart's five Dallas "Software Engineering Intern" rows carry different Workday `JR…` IDs — so the snapshot key would collapse them and **miss** real additions. Each table is its own watcher with its own `seen` set (`speedyapply/2027-SWE-College-Jobs#faang` / `#quant` / `#other`); the tables are disjoint by company category, so a URL never crosses tables, and separate seen-sets need no cross-table logic. The three watchers share the single `Speedyapply Summer Repo` label and stamp the category into `default_term`, so it renders in the alert's last field. Its file SHA advances often (the `Age` column re-computes daily), but re-parses never re-alert because the apply URLs are unchanged.

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

## 3) Zapply Repo (`zapplyjobs/Internships-2027` → `README.md`)

The README is six collapsible `<details>` category tables (Software Engineering, Data Science & AI, Hardware & Engineering, Product/Design/Research, Business & Operations, Other). **Only the first, `💻 Software Engineering`, is watched.** All Zapply job boards are produced by a shared README generator, so this 6-column format is stable. Each table has an identical header:

```md
| Company | Role | Location | Posted | Visa | **Apply** |
```

Column quirks (0-indexed after `strip('|').split('|')`):
- `[0]` **Company** — bold plaintext `**Name**` (no link); the `**` is stripped via `strip_bold=True`.
- `[1]` **Role** — plaintext, **truncated to ~40 chars with a literal `...`** when long.
- `[2]` **Location** — plaintext, also truncated with `...`.
- `[3]` **Posted** — always the literal `Recently` (no usable date) → not used.
- `[4]` **Visa** — always empty → not used.
- `[5]` **Apply** — `[<img src="images/apply.png" width="80" alt="Apply">](REAL_ATS_URL)`. `extract_apply_url` finds no `href=`, so it falls through to the `](url)` regex and captures the full ATS URL (Greenhouse / Workday / Ashby / Lever / SmartRecruiters / Oracle / etc.).

Config: `parser="markdown"`, `role_col=1`, `loc_col=2`, `apply_col=5`, `term_col=None` (stamps `default_term="Summer 2027"`), `min_cells=6`, `strip_bold=True`, `dedup="cumulative_url"`.

Parsing behavior:
- Shares `parse_markdown_rows` with Vansh; its keyword args override the column layout (defaults reproduce Vansh, so the Vansh call is untouched).
- The header row (`| Company | ... |`) is skipped by the `parts[0].lower().replace('*','') == 'company'` check; the `|---|` separator is skipped by the dash/colon check.
- The `<p align="center">…</p>` promo lines and `</details>` tag between the table and the next section are ignored (they don't start with `|`).
- Dedup is cumulative-URL, **not** snapshot diff — see "Exception: Zapply cumulative-URL dedup" above.

Example row → parsed output:

```md
| **ByteDance** | Software Engineer Intern (Applied Mac... | San Jose, California | Recently |  | [<img src="images/apply.png" width="80" alt="Apply">](https://joinbytedance.com/search/7533045355162044690) |
```
→ `["ByteDance", "Software Engineer Intern (Applied Mac...", "San Jose, California", "Summer 2027", "https://joinbytedance.com/search/7533045355162044690"]`

## 4) Speedyapply Repo (`speedyapply/2027-SWE-College-Jobs` → `README.md`)

The watched `README.md` is the **USA SWE Internships** page (New-Grad and International listings live in separate files — `NEW_GRAD_USA.md`, `INTERN_INTL.md`, `NEW_GRAD_INTL.md` — and are **not** watched). It holds three markdown pipe-tables, each wrapped in stable HTML-comment delimiters emitted by the generator. **All three are watched**, as three separate watcher entries sharing the `Speedyapply Summer Repo` label:

| Table | Markers | `apply_col` (0-idx) | `min_cells` | `default_term` |
|---|---|---|---|---|
| FAANG+ | `<!-- TABLE_FAANG_START -->` … `<!-- TABLE_FAANG_END -->` | `4` | `6` | `"FAANG+"` |
| Quant  | `<!-- TABLE_QUANT_START -->` … `<!-- TABLE_QUANT_END -->` | `4` | `6` | `"Quant"` |
| Other  | `<!-- TABLE_START -->` … `<!-- TABLE_END -->` | `3` | `5` | `"Other"` |

FAANG+/Quant headers are `Company \| Position \| Location \| Salary \| Posting \| Age` (6-col). The **Other** table drops the `Salary` column → `Company \| Position \| Location \| Posting \| Age` (5-col), which is why its `apply_col`/`min_cells` are one lower.

Column quirks (0-indexed after `strip('|').split('|')`):
- `[0]` **Company** — `<a href="companysite"><strong>Name</strong></a>`; `strip_html` removes the tags → clean `Name` (no `**`, so **no** `strip_bold`). The `href` here is the *company website*, not the apply link — but it is never read, because `extract_apply_url` runs only on the Posting cell.
- `[1]` **Position** — **full** role text (not truncated, unlike Zapply).
- `[2]` **Location** — e.g. `Mountain View, CA +29`.
- **Salary** (`$72/hr`, FAANG+/Quant only) — dropped.
- **Posting** — `<a href="REAL_ATS_URL"><img src="https://i.imgur.com/JpkfjIq.png" alt="Apply"/></a>`; `extract_apply_url` matches the `href=` and returns the ATS URL (Workday / Greenhouse / Ashby / Lever / SmartRecruiters / iCIMS / etc.).
- **Age** (`2d`, `17d`) — recomputed daily, dropped (not part of any key), so the file SHA advances often but re-parses never re-alert.

Config: `parser="markdown"`, `role_col=1`, `loc_col=2`, `term_col=None`, `dedup="cumulative_url"`, plus the per-table `apply_col`/`min_cells`/`default_term` above. Dedup is cumulative-URL, **not** snapshot diff — see "Exception: cumulative-URL dedup (Zapply + Speedyapply)" above.

Example row (FAANG+) → parsed output:

```md
| <a href="https://www.google.com"><strong>Google</strong></a> | Software Engineering Intern - MS - Summer 2027 | Mountain View, CA +29 | $72/hr | <a href="https://www.google.com/about/careers/applications/jobs/results/95141459539174086"><img src="https://i.imgur.com/JpkfjIq.png" alt="Apply" width="70"/></a> | 2d |
```
→ `["Google", "Software Engineering Intern - MS - Summer 2027", "Mountain View, CA +29", "FAANG+", "https://www.google.com/about/careers/applications/jobs/results/95141459539174086"]`

## Listing Identity and Change Classification

Listing key:

```text
(company, role, location, term_or_date)
```

`apply_url` is intentionally not part of the key **for the snapshot-diff sources** — companies sometimes rotate query strings.

**Zapply and Speedyapply are the exceptions:** they key by `apply_url` alone (Zapply's text fields are truncated/constant and collide; Speedyapply has many distinct openings sharing `(company, role, location)`) and compare against the cumulative `seen` set, not the previous snapshot. Query strings are kept intact because some ATS job IDs live in the query (e.g. Greenhouse `?gh_jid=`).

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
- Zapply Role/Location cells are truncated with a literal `...`, so alerts (and the Google-Sheet log) show the truncated role; the full title is not in the README. The apply URL is included for click-through.
- Zapply dedup assumes apply URLs are stable (verified over an 8h window). If Zapply ever appends rotating query params, duplicates could leak — observable via alert volume.
- Speedyapply lists many openings that share `(company, role, location)` and differ only by apply URL, so it (like Zapply) keys on the URL; a same-opening URL change (rare ATS re-issue) would alert again. Its `Age` column changes daily, so its file SHA advances frequently, and the three table-watchers each re-fetch the same ~53 KB README on a change — minor and accepted (grouping watchers by file would need code).

## Where to Update If Upstream Formats Change

Update these in `.github/workflows/watch-files.yml`:
- `WATCHERS[*].section_start` / `section_end` if section headings change.
- `parse_html_rows` if the Simplify cell layout changes (column count, nesting).
- `parse_markdown_rows` if Vansh's, Zapply's, **or Speedyapply's** column order changes — it is now column-configurable via keyword args (`role_col` / `loc_col` / `apply_col` / `term_col` / `default_term` / `min_cells` / `strip_bold`); the defaults preserve Vansh behavior.
- The `dedup == "cumulative_url"` branch and `URL_CAP` if Zapply's **or Speedyapply's** churn behavior or seen-set handling needs tuning.
- Speedyapply's `<!-- TABLE_*_START/END -->` comment markers (or the addition/removal of a category table) — update the three `speedyapply/2027-SWE-College-Jobs#*` watcher entries.
- `strip_html` / `extract_apply_url` for HTML entity or link-markup drift.
- `row_key` if the snapshot-path identity tuple needs to change (Zapply keys by URL and is unaffected).

Also update this document.
