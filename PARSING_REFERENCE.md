# Repo Watcher Parsing Reference

This document describes exactly what is being watched, how upstream file formats differ, and how commit diffs are parsed.

## Watched Repos

| Label | Repo | Branch | File | Format Type |
|---|---|---|---|---|
| Simplify Repo | `SimplifyJobs/Summer2026-Internships` | `dev` | `README-Off-Season.md` | HTML table embedded in markdown |
| Vansh Repo | `vanshb03/Summer2027-Internships` | `dev` | `OFFSEASON_README.md` | Markdown pipe table rows |

Source of truth for watcher config: `.github/workflows/watch-files.yml` in the `WATCHERS` list.

## State + Commit Comparison Flow

1. The workflow reads `.watcher_state.json`.
2. For each watched repo, it fetches latest commit SHA from:
   - `GET /repos/{owner}/{repo}/commits/{branch}`
3. It compares last seen SHA vs latest SHA.
4. If different, it fetches compare data from:
   - `GET /repos/{owner}/{repo}/compare/{last_sha}...{latest_sha}`
5. It filters `compare.files[]` to only the watched filename.
6. It parses `file.patch` text (unified diff) to find added/removed job rows.
7. It updates `.watcher_state.json` to latest SHA for that repo.

Important fields used from compare response:
- `files[].filename`
- `files[].patch`
- `html_url` (for compare link in alert)

## Unified Diff Shape (What Parsing Sees)

Each line in `patch` is typically prefixed by:
- `+` for added content
- `-` for removed content
- ` ` (space) for context

The parser ignores file headers such as:
- `+++ b/...`
- `--- a/...`

## Repo Format Details

## 1) Simplify Repo (`README-Off-Season.md`)

The job list is an HTML table with rows like:

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
- The parser scans diff lines for `<tr>`.
- It collects contiguous row lines with the same diff prefix (`+` or `-`).
- It extracts `<td>...</td>` cells in row order.
- It keeps rows with at least 4 cells.
- Current formatter consumes first 4 cells as:
  - company
  - role
  - location
  - term

Normalization:
- `<br>` variants are converted to spaces.
- Remaining tags are stripped.
- HTML entities like `&amp;`, `&lt;`, `&gt;` are decoded.

## 2) Vansh Repo (`OFFSEASON_README.md`)

The active list is a markdown pipe table. Typical row:

```md
| Company | Role | Location | <a href="..."><img alt="Apply"></a> | Apr 22 |
```

Parsing behavior:
- The parser reads only diff lines starting with `+` or `-`.
- It keeps lines whose body starts with `|`.
- It splits by `|` into cells.
- It skips separator rows like `| --- | --- | ... |`.
- Expected columns:
  - `0`: company
  - `1`: role
  - `2`: location
  - `3`: apply link markup
  - `4`: posted date
- Current parser drops column 3 and keeps `[0, 1, 2, 4]`.

Normalization:
- Same HTML/tag stripping path as Simplify parsing (via `strip_html`).

## Listing Identity and Change Classification

Current listing key:

```text
(company, role, location, term_or_date)
```

Classification logic:
- `added_keys`: keys from added rows
- `removed_keys`: keys from removed rows
- `moved_keys`: intersection (`added_keys & removed_keys`)

Interpretation:
- Added = rows in `added` not in `moved_keys`
- Removed = rows in `removed` not in `moved_keys`
- Moved = rows appearing in both sides by key

This prevents section reorder/noise from being treated as real additions/removals.

## Practical Diff Examples

Simplify-style add in patch:

```diff
<tr>
<td><strong><a href="https://simplify.jobs/c/Example">Example Co</a></strong></td>
<td>Software Engineer Intern</td>
<td>San Francisco, CA</td>
<td>Spring 2027</td>
<td><div align="center"><a href="https://jobs.example.com/apply"><img alt="Apply"></a></div></td>
<td>0d</td>
</tr>
```

Vansh-style add in patch:

```diff
| Example Co | Software Engineer Intern | San Francisco, CA | <a href="https://jobs.example.com/apply"><img alt="Apply"></a> | Apr 22 |
```

## Known Parsing Constraints

- `compare.files[].patch` can be missing for very large diffs, binary changes, or truncation.
- If watched file is not in `compare.files[]`, watcher treats it as no relevant change.
- Only the watched filename is parsed, even if other files changed in the same compare.
- HTML structure drift (missing `<td>` close tags, different row wrappers) can break extraction.
- Markdown rows containing unescaped `|` inside cell content can shift columns.

## Where to Update If Upstream Formats Change

Update these functions in `.github/workflows/watch-files.yml`:
- `collect_row`
- `parse_html_table`
- `parse_markdown_table`
- `strip_html`
- `key` and `build_change_lines` if classification semantics change

Also update this document when parser assumptions change.
