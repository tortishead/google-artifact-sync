# ci.android.com artifact fetcher

Downloads `signed/signed-gms_car_prod_<arch>-<build>.zip` from ci.android.com using an ID
token from your corporate Google login.

## Setup

Only dependency is Playwright (uses your installed Google Chrome):

```sh
python3 -m pip install --user --break-system-packages playwright
```

## Usage

```sh
python3 fetch_artifact.py <build_id> [<build_id>...] [-a x86_64] [-a arm64] [-o downloads/]
```

- First run: a Chrome window opens on the build page. Sign in with your corporate account,
  then press Enter in the terminal. The session is saved in `~/.cache/ci-fetch/profile`.
- Later runs get a fresh token from the saved session without opening a window.
- `--arch` defaults to `x86_64`; repeat it to get both. Target is `gms_car_prod_<arch>-user`.
- `--login` makes you sign in again. Interrupted downloads resume from the `.part` file.

## Opening a Bitbucket PR with a new build

```sh
cp .env.example .env && chmod 600 .env   # fill in GMS_REPO_URL and BITBUCKET_TOKEN once
python3 update_repo.py <new_build> [--base-build <current_build>] [-t main] [-r reviewer]
```

`.env` is git-ignored; variables already set in the shell override it. The token should be a
Bitbucket Server HTTP access token scoped to the repo, with write permission.

Downloads the arches the map uses, and opens a PR from `gms/update-<build>` into the target
branch.

`gms-map.txt` (or `--map <file>`) says where each zip file goes in the repo and how it merges:

```
# <repo path>          <arch>/<path in zip>     <strategy>
apps/google/           arm64/google/            rebase
apps_x86_64/google/    x86_64/google/           rebase
apps/google/apps/WebViewGoogle.apk  arm64/google/apps/WebViewGoogle.apk  replace
```

- A path ending in `/` maps a directory, otherwise one file. The line with the longest
  matching zip path wins. Every file in the zips must be mapped, so new files from Google
  can't be missed silently.
- `rebase`: local edits are kept (described below). `replace`: Google's file wins, local
  edits to it are overwritten, files Google removed are deleted. Local-only files are kept
  either way. The PR description lists the overwritten files.

To write a first map, scan the repo against the build it was made from:

```sh
python3 scan_repo.py <current_build> [-t main] [-s rebase] [-o gms-map.txt] [--force]
```

It finds each zip file in the target branch by content (git blob or LFS object), or by path
when it was edited locally, and folds the matches into directory lines plus one line per file
that lives elsewhere. Zip files not found in the repo are listed as comments, so
`update_repo.py` won't run until they're mapped. Check the strategies before using the map.

With `rebase`, local edits in the repo are kept:

- Every imported build is also committed unmodified, laid out per the map, on the
  `gms-pristine` branch and tagged `gms-pristine/<build>-<map hash>`. Changing the repo
  paths in the map makes new snapshots (changing only a strategy doesn't). Unchanged APKs
  are the same git objects as on `main`, so this costs almost no space.
- `.gms-build` on the target branch records the build it is on. The PR applies only
  pristine(old) → pristine(new) to the target branch (a 3-way cherry-pick), so lines you changed
  stay changed. The PR description lists Google's changes and the files carrying local edits.
- **First run:** the repo has no `.gms-build` yet, so pass `--base-build` with the build the
  initial commit came from. Not needed afterwards. Squash-merging the PR is fine.
- **Conflict** (you and Google changed the same lines of a `rebase` file): the tool stops and lists the files.
  Resolve them in the working clone (`~/.cache/ci-fetch/gms-repo`), `git add`,
  `git cherry-pick --continue`, then rerun the same command with `--resume`.
- `--no-pr` prepares the branch locally only. With an SSH remote, also pass
  `--bitbucket-url https://bitbucket.example.com`.
- If the repo uses Git LFS (`.gitattributes`), `git-lfs` must be installed.
