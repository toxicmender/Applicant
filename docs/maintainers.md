# Maintainers: the steps only a repository admin can take

Everything else in this repository is done through commits and pull requests.
These three steps live in the GitHub settings or need push rights to things a pull
request cannot touch. Take them in this order: the rename first, because the other
two name the branch.

## 1. Rename the default branch from `master` to `main`

**In GitHub:** Settings → General → *Default branch* → the pencil (rename) next to
`master` → `main` → *Rename branch*.

Or, with the GitHub CLI and admin rights:

```
gh api -X POST repos/toxicmender/Applicant/branches/master/rename -f new_name=main
```

Renaming (rather than creating a `main` beside `master`) is what makes the rest
automatic. GitHub:

- retargets every open pull request whose base was `master` - #6 included;
- moves the branch protection rules from `master` to `main`;
- redirects web links and `git` operations that still name `master`;
- keeps the history, so no commit hash changes and tags stay where they are.

Nothing in the repository itself needs editing: both workflows run on every branch
(`on: push` / `pull_request` with no branch filter), and no README link names the
branch. The places that still say `master` - `docs/architecture-plan.md`,
`docs/ci-plan.md` and the CHANGELOG's account of the broken merge - describe what the
branch was called at the time, and stay as written.

**Every existing clone** then points at a branch that no longer exists. Once, in
each clone:

```
git branch -m master main
git fetch origin
git branch -u origin/main main
git remote set-head origin -a
```

`git remote set-head` updates what `origin/HEAD` points at, so tools that ask for
"the default branch" get `main`. A clone that has local work on other branches keeps
it; only the `master` branch is renamed.

## 2. Make `status` a required check on `main`

Settings → Branches → the rule for `main` (moved there by the rename; add one if
there is none) → *Require status checks to pass before merging* → search for and
add **`status`**.

`status` is the one check to require: it is the last job in `ci.yml`, it reads every
other job's result, and it fails when the type check, any test, the coverage floor
or the real-browser tier does. Without this
setting a red CI run is advisory, which is how `master` once stayed broken with
every check green (see `docs/architecture-plan.md`, D1-D3).

A commit pushed by the `format` workflow gets its own `ci` run, dispatched by that
workflow, so a formatted head is never left waiting for a `status` that will not
come.

## 3. Tag the release

After the release pull request is merged, on the commit that is now the head of
`main` (a squash merge makes a new one; a merge commit keeps the branch's):

```
git fetch origin
git tag -a v0.2.0 origin/main -m "applicant 0.2.0"
git push origin v0.2.0
```

Or: Releases → *Draft a new release* → tag `v0.2.0`, target `main`, with the 0.2.0
section of `CHANGELOG.md` as the notes. If the tag goes on a later day than the
CHANGELOG entry's date, change that date first.

## Refreshing a saved page

`tests/test_live_pages.py` reads each browser-driven site from a page in
`tests/fixtures/pages`, written to the markup the code expects. When a site changes,
save the real page from a machine that can reach it, and run the tier against it:

```
uv run python tools/record_page.py https://www.naukri.com/python-jobs \
    tests/fixtures/pages/naukri/search-1.html --capture '/jobapi/v3/search' tests/fixtures/pages/naukri/search-1.json
uv run pytest tests/test_live_pages.py
```

`--show` opens a visible browser to clear a bot check or sign in first; `--redact`
strips anything else personal (email addresses always are). Read the files before
committing them. A failure after a refresh is the site having moved under the
selectors - which is what the tier is for. Locally, if the bundled Chromium does not
match the installed Playwright, set `APPLICANT_TEST_CHROMIUM` to a Chromium binary.
