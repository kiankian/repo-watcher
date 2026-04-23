# repo-watcher

A GitHub Actions workflow that watches job-board repos and sends Telegram alerts when new listings appear.

Currently watches:

- `SimplifyJobs/Summer2026-Internships` — `README-Off-Season.md` on `dev`
- `vanshb03/Summer2027-Internships` — `OFFSEASON_README.md` on `dev`

State is persisted in `.watcher_state.json` (last-seen SHA per repo). The workflow commits that file back to this repo on every run that advances a SHA.

## Pushing a local commit when the remote has advanced

Because the workflow pushes `.watcher_state.json` updates on its own schedule, you will frequently find that the remote has moved ahead of your local branch. If you try to push on top of that, the branches will appear divergent.

**Always rebase your local commits on top of the remote before pushing:**

```sh
git pull --rebase origin main
git push origin main
```

This places your local commits on top of the automated state-update commits from the runner, keeping a linear history.

### Avoid

- `git pull` without `--rebase` — creates noisy merge commits that interleave with the automated state updates.
- `git push --force` — will overwrite the watcher's state commits and cause the next run to re-alert on stale diffs.
- Resolving "divergent branches" errors by `git reset --hard` — discards in-flight work.

### If a rebase conflicts on `.watcher_state.json`

Keep the **remote** version (the runner's SHA is more recent than yours):

```sh
git checkout --theirs .watcher_state.json
git add .watcher_state.json
git rebase --continue
```


# TODO
- [ ] Better Formatting 
- [ ] Don't show removed jobs and moved jobs
- [ ] More information in update
    - [ ] Add Link