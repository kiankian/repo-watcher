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

Zapply additionally carries `role_col=1`, `loc_col=2`, `min_cells=6`, and `strip_bold=True`. Speedyapply carries `role_col=1`, `loc_col=2`, and per-table `apply_col`/`min_cells` (FAANG+/Quant are 6-col → `apply_col=4`, `min_cells=6`; Other drops the Salary column → `apply_col=3`, `min_cells=5`); it needs **no** `strip_bold` because its company cells are HTML `<strong>` (already removed by `strip_html`), not markdown `**`. These all feed the now-column-configurable `parse_markdown_rows` (its defaults reproduce the Vansh layout, so the Vansh call is unchanged).

## State + Delivery Flow

Every source now uses one mechanism. The snapshot-diff / cumulative-URL split described in earlier
revisions of this document was replaced on 2026-07-30; see the Delivery guarantee section of
`README.md` for the rationale.

Each entry in `.watcher_state.json` has the same shape:

```json
{ "last_sha": "...",
  "seen": ["<identity>", "..."],
  "seen_legacy_urls": ["<url>", "..."],
  "bootstrap": ["<identity>", "..."],
  "bootstrap_legacy_urls": ["<url>", "..."],
  "last_row_count": 28,
  "outbox": [[["company", "role", "location", "term", "url"], "<identity>", 1]] }
```

1. Fetch the latest commit SHA: `GET /repos/{owner}/{repo}/commits/{branch}`.
2. Skip only when `last_sha == latest_sha`, `seen` is populated, **and** the `outbox` is empty. A
   queued job must not wait on an unrelated upstream commit, so pending work always proceeds. A
   dry run never short-circuits either — the live watcher stores the current head every minute, so
   a rehearsal would otherwise parse nothing and report success.
3. Fetch the file at `latest_sha` from `raw.githubusercontent.com`. A 404 here means the watched
   file was renamed or deleted: report `⚠️ fetch-failed`, mark the run unhealthy, and still drain
   the outbox — queued rows are self-contained and must not be stranded behind a dead source.
4. Slice the section with `section_start` / `section_end`.
5. Parse rows into `[company, role, location, term_or_category, apply_url]`, resolving `↳` to the
   previous row's company.
6. **Empty-parse guard:** zero rows means marker or format drift. Report `⚠️ zero-rows`, mark the
   run unhealthy, never seed an empty set, never advance the SHA — but still drain the outbox.
7. **Silent bootstrap:** no `seen` yet → seed identities from the current rows into `bootstrap`
   and alert nothing. `seen` stays empty because nothing was delivered, and both legacy sets stay
   empty, or a requisition relisted for a new season could never alert.
8. Otherwise select rows whose identity is absent from `seen ∪ bootstrap` and whose URL is absent
   from `seen_legacy_urls ∪ bootstrap_legacy_urls` (`suppression_sets`). Prepend anything already
   in the `outbox` so the oldest work drains first.
9. Deliver, capped at `BURST_CAP` attempts and the whole-run time budget.
10. Union the confirmed identities into `seen` (capped at `SEEN_CAP`, oldest evicted). Undelivered
    rows go back to the `outbox`. Advance `last_sha` only if a snapshot was actually parsed.

Why an append-only identity set rather than a snapshot diff:

- A snapshot that is replaced wholesale forgets whatever vanished from it. These tables shrink and
  re-expand constantly, and each shrink caused the forgotten rows to re-alert on return — measured
  at roughly 30% of Simplify alerts before the change.
- Section filtering stays trivial: slice by markers, ignore everything outside.
- `↳` ambiguity is still resolved deterministically by walking rows in order.
- Re-orderings, `↳`-flips and active↔inactive `🔒` flips remain silent: same identity, same row.
- Cross-section moves into SWE, and inactive→active re-listings, still surface as genuinely new
  identities.

`seen_legacy_urls` holds bare apply URLs inherited from the pre-2026-07-30 cumulative-URL sources
(Zapply and Speedyapply). Those records carried no term, so they can only be matched on URL. The
set is static and can be dropped once those listings have aged out.

### Why the bootstrap mute is stored separately

A row is silenced for one of two reasons, and until 2026-08-03 the state could not tell them
apart: both landed in `seen`. The distinction matters because only one of them is recoverable.

| set | meaning | released by |
|---|---|---|
| `seen`, `seen_legacy_urls` | delivered — the alert was sent and confirmed | never; re-sending is a duplicate |
| `bootstrap`, `bootstrap_legacy_urls` | muted at initialization — never sent | `release_bootstrap` dispatch input |

Both suppress, so the split changes nothing a run does on its own. What it changes is that the
mute is now countable (`bootstrap_muted` in the run log) and reversible.

The failure it addresses: Zapply's SWE table is capped at ~100 rows and re-sorted by recency, so
bootstrapping it on 2026-07-21 muted the entire visible board. Thirteen days later 51 of those
listings were still posted, still unsent, and nothing in the state recorded that they had never
gone out — diagnosing it meant replaying 487 upstream commits and cross-checking `.bot_state.json`.
ByteDance's three San Jose SWE rows were in that set; the only Zapply messages ever sent for them
were the dead-`#`-link duplicates from the 2026-08-01 upstream breakage.

**Migration.** A source already on the unified shape has its legacy URLs split by delivery: any URL
with no record in `.bot_state.json` was never sent to anyone, so it is mute rather than history and
moves to `bootstrap_legacy_urls`. Union suppression is unchanged by the move, so **the first run
after the upgrade alerts nothing** — verified against the committed state files in
`tests/test_core.py::test_migrating_the_real_state_files_is_safe`. The split is only as accurate as
`.bot_state.json`: a delivery whose record has been evicted reads as never-delivered, and the cost
of that error is one duplicate if the mute is later released.

**Re-alert implications of releasing.** Releasing empties the two bootstrap sets and touches
nothing else. Released rows are not replayed — they are simply no longer suppressed, so the next
parse selects the ones still listed as new and they leave through the outbox at `BURST_CAP` per
run. Rows that have left the board produce nothing. `seen` is untouched, so a delivered alert
cannot repeat, and re-dispatching a release is a no-op because the sets are already empty.

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

Config: `parser="markdown"`, `role_col=1`, `loc_col=2`, `apply_col=5`, `term_col=None` (stamps `default_term="Summer 2027"`), `min_cells=6`, `strip_bold=True`.

Parsing behavior:
- Shares `parse_markdown_rows` with Vansh; its keyword args override the column layout (defaults reproduce Vansh, so the Vansh call is untouched).
- The header row (`| Company | ... |`) is skipped by the `parts[0].lower().replace('*','') == 'company'` check; the `|---|` separator is skipped by the dash/colon check.
- The `<p align="center">…</p>` promo lines and `</details>` tag between the table and the next section are ignored (they don't start with `|`).
- Its Role/Location are truncated and its Posted column is the constant `Recently`, so the apply URL is the only field here that identifies a row. The shared identity leads with the URL for exactly this reason.

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

Config: `parser="markdown"`, `role_col=1`, `loc_col=2`, `term_col=None`, plus the per-table `apply_col`/`min_cells`/`default_term` above. Many of its openings share `(company, role, location)` and differ only by apply URL, which is why the identity includes the URL as well as the text.

Example row (FAANG+) → parsed output:

```md
| <a href="https://www.google.com"><strong>Google</strong></a> | Software Engineering Intern - MS - Summer 2027 | Mountain View, CA +29 | $72/hr | <a href="https://www.google.com/about/careers/applications/jobs/results/95141459539174086"><img src="https://i.imgur.com/JpkfjIq.png" alt="Apply" width="70"/></a> | 2d |
```
→ `["Google", "Software Engineering Intern - MS - Summer 2027", "Mountain View, CA +29", "FAANG+", "https://www.google.com/about/careers/applications/jobs/results/95141459539174086"]`

## Listing Identity and Change Classification

```text
identity = (apply_url or "NOURL") |company|role|location|term  #occurrence
```

Every field participates, because either half alone collapses distinct openings:

- **URL alone is not enough.** Boards sometimes publish a generic link shared by several rows (a
  bare careers page, a Greenhouse embed). If one of those openings is replaced while the row count
  holds, occurrence numbering hands the replacement an identity that is already seen.
- **Text alone is not enough.** Copart posts several Dallas "Software Engineer Intern" rows that
  differ only by Workday `JR…` id; keyed on text they collapse and all but one are never alerted.
- **`term`** lets a requisition relisted for a new season through.
- **`#occurrence`** separates rows identical in every field — Kudu Dynamics lists the same
  URL-less role three times in the Vansh table.

Query strings are kept intact: some ATS job ids live there (e.g. Greenhouse `?gh_jid=`). The cost
is that a rotated tracking parameter reads as a new opening and alerts once more. That is the
accepted direction — every residual failure here produces a duplicate rather than a miss.

Including the text costs a duplicate whenever upstream edits a role or location in place. Measured
across 2,060 state snapshots spanning 18 days and 475 distinct `(source, URL)` pairs, that happened
zero times.

Classification:

- Alert a row iff its identity is absent from `seen ∪ bootstrap` **and** its URL is absent from
  `seen_legacy_urls ∪ bootstrap_legacy_urls`.
- `seen` is a union that only ever grows, so a shrinking parse cannot resurrect old listings.
- An identity is added to `seen` only after Telegram confirms the message. Undelivered rows are
  persisted in the `outbox` and retried, so a failed send is never silently dropped.
- Removals (closures) are ignored — only additions alert.
- Active↔Inactive toggles in Simplify never alert: the inactive block is outside the parsed range,
  and a re-listing returns with an identity already in `seen`.

The callback hash carried by each `✅ Applied` button is separate from the identity —
`job_hash(entry, label, occurrence)`, unchanged at occurrence 1 so that buttons issued before this
scheme still resolve. Do not alter its inputs.

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
- The identity assumes apply URLs are reasonably stable. If a board starts appending rotating query params, each rotation reads as a new opening and duplicates leak — observable via alert volume and the `logs/alerts-*.jsonl` record.
- Speedyapply lists many openings sharing `(company, role, location)` and differing only by apply URL, which the URL half of the identity separates; a same-opening URL change (rare ATS re-issue) would alert again. Its `Age` column changes daily, so its file SHA advances frequently, and the three table-watchers each re-fetch the same ~53 KB README on a change — minor and accepted (grouping watchers by file would need code). `Age` is not part of any identity: Speedyapply stamps its category into `default_term` instead.

## Where to Update If Upstream Formats Change

Update these in `.github/workflows/watch-files.yml`:
- `WATCHERS[*].section_start` / `section_end` if section headings change.
- `parse_html_rows` if the Simplify cell layout changes (column count, nesting).
- `parse_markdown_rows` if Vansh's, Zapply's, **or Speedyapply's** column order changes — it is now column-configurable via keyword args (`role_col` / `loc_col` / `apply_col` / `term_col` / `default_term` / `min_cells` / `strip_bold`); the defaults preserve Vansh behavior.
- `SEEN_CAP`, `OUTBOX_CAP`, `BURST_CAP` or `RUN_BUDGET_SECONDS` in the workflow if retention, queue depth, send volume or run duration needs tuning.
- Speedyapply's `<!-- TABLE_*_START/END -->` comment markers (or the addition/removal of a category table) — update the three `speedyapply/2027-SWE-College-Jobs#*` watcher entries.
- `strip_html` / `extract_apply_url` for HTML entity or link-markup drift.
- `identity_stem` / `row_identity` in `watcher/core.py` if the identity needs to change. Note this is a state-format change: existing `seen` entries are in the old format and would all look new, so it needs a dual-format check or a migration in `migrate_state`.
- `tests/` alongside any of the above — see `TESTING.md`. `row_key` still exists in `watcher/core.py` but is retained only to document the superseded snapshot key.

Also update this document, `AGENTS.md`, and the Delivery guarantee section of `README.md`.
