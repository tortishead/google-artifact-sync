#!/usr/bin/env python3
"""Open a Bitbucket Server pull request that moves the repo to a new gms_car_prod build.

gms-map.txt (or --map) says where each file of the arm64 and x86_64 zips goes in
the repo, and whether local edits to it are kept (rebase) or overwritten
(replace). Every imported build is also committed unmodified, laid out per the
map, on the gms-pristine branch (tagged gms-pristine/<build>-<map hash>), and the
update applied to the target branch is only the difference between the pristine
copy of the build the repo is on (recorded in .gms-build) and the new one. Where
a local edit and a Google change touch the same lines of a rebase file, the
cherry-pick stops with a conflict to resolve by hand.

The first run needs --base-build: the build the target branch was created from.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import NamedTuple
from urllib.error import HTTPError
from urllib.parse import urlparse

from fetch_artifact import ARCHES, DEFAULT_PROFILE, AuthRequired, fetch_builds

BUILD_FILE = ".gms-build"
STRATEGIES = ("rebase", "replace")
PRISTINE_BRANCH = "gms-pristine"
PRISTINE_TAG = "gms-pristine/{build}-{map_hash}"
PR_BRANCH = "gms/update-{build}"
DEFAULT_REPO_DIR = Path.home() / ".cache" / "ci-fetch" / "gms-repo"
DEFAULT_DOWNLOADS = Path(__file__).resolve().parent / "downloads"
ENV_FILE = Path(__file__).resolve().parent / ".env"
DEFAULT_MAP = Path(__file__).resolve().parent / "gms-map.txt"


class Rule(NamedTuple):
    repo: str      # path in the repo; ends with / for a directory
    arch: str
    zip_path: str  # path inside that arch's zip; ends with / (or is empty) for a directory
    strategy: str

    @property
    def is_dir(self):
        return self.repo.endswith("/")


def load_map(path):
    rules = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        fields = line.split("#", 1)[0].split()
        if not fields:
            continue
        where = f"{path}:{n}"
        if len(fields) != 3:
            sys.exit(f"{where}: expected <repo path> <arch>/<path in zip> <strategy>")
        repo, src, strategy = fields
        arch, _, zip_path = src.partition("/")
        if arch not in ARCHES:
            sys.exit(f"{where}: unknown arch {arch!r} (expected one of {', '.join(ARCHES)})")
        if strategy not in STRATEGIES:
            sys.exit(f"{where}: unknown strategy {strategy!r} (expected {' or '.join(STRATEGIES)})")
        if repo.endswith("/") != src.endswith("/"):
            sys.exit(f"{where}: map a directory to a directory (both ending in /) or a file to a file")
        if not zip_path and not src.endswith("/"):
            sys.exit(f"{where}: missing path inside the {arch} zip")
        for part in (repo, zip_path):
            if part.startswith("/") or ".." in Path(part).parts or part in ("/", "./"):
                sys.exit(f"{where}: invalid path {part!r}")
        if repo.rstrip("/") == BUILD_FILE:
            sys.exit(f"{where}: {BUILD_FILE} is reserved")
        rules.append(Rule(repo, arch, zip_path, strategy))
    if not rules:
        sys.exit(f"{path}: no mappings")
    for key in ("repo", "zip"):
        seen = [(r.repo,) if key == "repo" else (r.arch, r.zip_path) for r in rules]
        if len(set(seen)) != len(seen):
            sys.exit(f"{path}: the same {key} path is mapped twice")
    return rules


def map_hash(rules):
    """Identifies the repo layout the map produces (strategies don't change it)."""
    layout = sorted(f"{r.repo}\0{r.arch}\0{r.zip_path}" for r in rules)
    return hashlib.sha1("\n".join(layout).encode()).hexdigest()[:8]


def _longest(rules, path, attr):
    def matches(r):
        prefix = getattr(r, attr)
        return path.startswith(prefix) if r.is_dir else path == prefix
    return max((r for r in rules if matches(r)), key=lambda r: len(getattr(r, attr)), default=None)


def rule_for_zip_entry(rules, arch, name):
    return _longest([r for r in rules if r.arch == arch], name, "zip_path")


def rule_for_repo_path(rules, path):
    return _longest(rules, path, "repo")


def load_env(path):
    """Set KEY=value lines from path as environment variables, unless already set."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class Git:
    def __init__(self, repo):
        self.repo = repo

    def __call__(self, *args, env=None, check=True, quiet=False, input=None):
        full_env = {**os.environ, **env} if env else None
        res = subprocess.run(["git", "-C", str(self.repo), *args], env=full_env, input=input,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL if quiet else None,
                             text=True)
        if check and res.returncode:
            raise SystemExit(f"git {' '.join(args)} failed (exit {res.returncode})")
        return res.stdout.strip() if check else res

    def exists(self, rev):
        return self("cat-file", "-e", rev, check=False, quiet=True).returncode == 0

    def show(self, rev, path):
        res = self("show", f"{rev}:{path}", check=False, quiet=True)
        return res.stdout if res.returncode == 0 else None

    def files(self, rev):
        return set(filter(None, self("ls-tree", "-r", "-z", "--name-only", rev).split("\0")))


def prepare_clone(repo_url, repo_dir, target):
    git = Git(repo_dir)
    if not (repo_dir / ".git").exists():
        if not repo_url:
            sys.exit(f"{repo_dir} is not a clone yet; pass --repo-url")
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--no-checkout", repo_url, str(repo_dir)], check=True)
    else:
        # An empty index means nothing was checked out yet (a fresh --no-checkout clone).
        if git("ls-files") and git("status", "--porcelain", "--untracked-files=no"):
            sys.exit(f"{repo_dir} has uncommitted changes; resolve or reset them first")
        git("fetch", "--prune", "--tags", "origin")
    origin_target = f"origin/{target}"
    if not git.exists(origin_target):
        sys.exit(f"target branch {target} not found on origin")
    if git.exists(f"origin/{PRISTINE_BRANCH}"):
        git("branch", "-f", PRISTINE_BRANCH, f"origin/{PRISTINE_BRANCH}")

    if "filter=lfs" in (git.show(origin_target, ".gitattributes") or ""):
        if git("lfs", "version", check=False).returncode:
            sys.exit("the repo uses Git LFS, but git-lfs is not installed (brew install git-lfs)")
        git("lfs", "install", "--local")
    return git


def extract(zip_path, arch, rules, tree_dir):
    """Write the zip's files to tree_dir at the repo paths the map gives them."""
    unmapped, written = [], {}
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            if info.is_dir():
                continue
            if ".." in Path(name).parts or Path(name).is_absolute():
                sys.exit(f"{zip_path}: unsafe entry {name}")
            rule = rule_for_zip_entry(rules, arch, name)
            if rule is None:
                unmapped.append(f"{arch}/{name}")
                continue
            dest = rule.repo + name[len(rule.zip_path):]
            if dest in written:
                sys.exit(f"{written[dest]} and {arch}/{name} both map to {dest}")
            if rule_for_repo_path(rules, dest) != rule:
                sys.exit(f"{arch}/{name} maps to {dest}, which another map line claims; "
                         f"make the map lines not overlap that way")
            written[dest] = f"{arch}/{name}"
            out = tree_dir / dest
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
    if unmapped:
        sys.exit(f"{zip_path}: {len(unmapped)} files are not in the map, e.g.:\n  "
                 + "\n  ".join(unmapped[:20]))


def pristine_snapshot(git, build, downloads, args, rules):
    """Return the tag holding the unmodified build, committing it if it doesn't exist yet."""
    tag = PRISTINE_TAG.format(build=build, map_hash=map_hash(rules))
    if git.exists(f"refs/tags/{tag}"):
        return tag

    arches = sorted({r.arch for r in rules})
    try:
        zips, failed = fetch_builds([build], arches, downloads, args.profile, args.login)
    except AuthRequired as e:
        sys.exit(f"Login failed: {e}")
    if failed:
        sys.exit(f"could not download build {build}")

    print(f"Committing pristine build {build} to {PRISTINE_BRANCH}")
    with tempfile.TemporaryDirectory(dir=git.repo.parent) as tmp:
        tree_dir = Path(tmp) / "tree"
        for arch in arches:
            extract(zips[build, arch], arch, rules, tree_dir)
        # Same attributes as the target branch, so LFS tracking applies to the snapshot too.
        attrs = git.show(f"origin/{args.target}", ".gitattributes")
        if attrs:
            (tree_dir / ".gitattributes").write_text(attrs)
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git(f"--work-tree={tree_dir}", "add", "-A", "-f", ".", env=env)
        tree = git("write-tree", env=env)

    parent = ["-p", PRISTINE_BRANCH] if git.exists(f"refs/heads/{PRISTINE_BRANCH}") else []
    commit = git("commit-tree", tree, *parent, "-m",
                 f"Pristine gms_car_prod build {build} (map {map_hash(rules)})")
    git("update-ref", f"refs/heads/{PRISTINE_BRANCH}", commit)
    git("tag", tag, commit)
    return tag


def tree_with_build_file(git, tag, build):
    """The snapshot's tree plus a .gms-build file naming the build (None: no such file)."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git("read-tree", f"{tag}^{{tree}}", env=env)
        git("rm", "--cached", "-q", "--ignore-unmatch", ".gitattributes", env=env)
        if build is not None:
            blob = subprocess.run(["git", "-C", str(git.repo), "hash-object", "-w", "--stdin"],
                                  input=f"{build}\n", stdout=subprocess.PIPE, text=True,
                                  check=True).stdout.strip()
            git("update-index", "--add", "--cacheinfo", f"100644,{blob},{BUILD_FILE}", env=env)
        return git("write-tree", env=env)


def update_trees(git, old_tag, new_tag, old_build, new_build, base_has_build_file):
    old_tree = tree_with_build_file(git, old_tag, old_build if base_has_build_file else None)
    new_tree = tree_with_build_file(git, new_tag, new_build)
    return old_tree, new_tree


def replaced_files(git, rules, old_tree, new_tree):
    """Files under replace lines: (those in the new build, those Google removed)."""
    is_replace = lambda f: getattr(rule_for_repo_path(rules, f), "strategy", None) == "replace"
    old, new = git.files(old_tree), git.files(new_tree)
    return sorted(filter(is_replace, new)), sorted(filter(is_replace, old - new))


def apply_update(git, rules, old_tree, new_tree, old_build, new_build, branch, target):
    """Cherry-pick the pristine old->new change onto a fresh branch from the target,
    then make the replace files exactly the new build's."""
    old_commit = git("commit-tree", old_tree, "-m", f"gms_car_prod build {old_build}")
    message = (f"Update gms_car_prod to build {new_build}\n\n"
               f"Previous build: {old_build}\n"
               f"Source: ci.android.com signed-gms_car_prod_{{arm64,x86_64}}-{new_build}.zip")
    delta = git("commit-tree", new_tree, "-p", old_commit, "-m", message)

    git("checkout", "-q", "-B", branch, f"origin/{target}")
    picked = git("cherry-pick", delta, check=False).returncode == 0

    keep, drop = replaced_files(git, rules, old_tree, new_tree)
    literal = {"GIT_LITERAL_PATHSPECS": "1"}
    if keep:
        git("checkout", delta, "--pathspec-from-file=-", "--pathspec-file-nul",
            input="\0".join(keep), env=literal)
    if drop:
        git("rm", "-q", "-f", "--ignore-unmatch", "--pathspec-from-file=-", "--pathspec-file-nul",
            input="\0".join(drop), env=literal)

    if picked:
        if git("diff", "--cached", "--quiet", check=False).returncode:
            git("commit", "-q", "--amend", "--no-edit")
        return
    conflicts = git("diff", "--name-only", "--diff-filter=U")
    if not conflicts:
        git("-c", "core.editor=true", "cherry-pick", "--continue")
        return
    sys.exit(f"\nLocal edits conflict with build {new_build} in:\n  "
             + conflicts.replace("\n", "\n  ")
             + f"\n\nResolve them in {git.repo}, then run `git add <files>` and"
             f" `git cherry-pick --continue` there, and rerun this command with --resume.")


def bitbucket_coords(remote_url, base_override):
    """(REST base URL, project key, repo slug) from a Bitbucket Server clone URL."""
    u = urlparse(remote_url if "://" in remote_url else "ssh://" + remote_url.replace(":", "/", 1))
    m = re.match(r"(?P<ctx>.*?)/(?:scm/)?(?P<project>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$", u.path)
    if not m:
        sys.exit(f"cannot parse Bitbucket project/repo from {remote_url}")
    if base_override:
        base = base_override.rstrip("/")
    elif u.scheme in ("http", "https"):
        base = f"{u.scheme}://{u.hostname}{f':{u.port}' if u.port else ''}{m['ctx']}"
    else:
        sys.exit("cannot derive the Bitbucket web URL from an SSH remote; pass --bitbucket-url")
    return base, m["project"], m["repo"]


def create_pr(base, project, repo, token, branch, target, title, description, reviewers):
    ref = lambda name: {"id": f"refs/heads/{name}",
                        "repository": {"slug": repo, "project": {"key": project}}}
    body = {"title": title, "description": description,
            "fromRef": ref(branch), "toRef": ref(target),
            "reviewers": [{"user": {"name": r}} for r in reviewers]}
    req = urllib.request.Request(
        f"{base}/rest/api/1.0/projects/{project}/repos/{repo}/pull-requests",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            pr = json.load(resp)
        return pr["links"]["self"][0]["href"], True
    except HTTPError as e:
        err = json.loads(e.read() or b"{}").get("errors", [{}])[0]
        existing = err.get("existingPullRequest")
        if e.code == 409 and existing:
            return existing["links"]["self"][0]["href"], False
        sys.exit(f"Creating the pull request failed: HTTP {e.code}: {err.get('message', e.reason)}")


def describe(git, rules, old_tree, new_tree, old_build, new_build, target):
    paths = sorted({r.repo for r in rules})
    changes = git("diff", "--name-status", "--no-renames", old_tree, new_tree, "--", *paths)
    lines = changes.splitlines()
    shown = "\n".join(lines[:200]) + (f"\n... and {len(lines) - 200} more" if len(lines) > 200 else "")
    edited = lambda rev: git("diff", "--name-only", "--no-renames", new_tree, rev, "--", *paths).splitlines()
    kept = edited("HEAD")
    keep, drop = replaced_files(git, rules, old_tree, new_tree)
    overwritten = sorted(set(edited(f"origin/{target}")).intersection(keep + drop) - set(kept))
    text = (f"Updates gms_car_prod from build {old_build} to {new_build}.\n\n"
            f"Changed by Google ({len(lines)} files):\n```\n{shown or '(none)'}\n```\n")
    if kept:
        text += f"\nLocal edits kept (differ from the pristine build):\n```\n" + "\n".join(kept) + "\n```\n"
    if overwritten:
        text += (f"\nLocal edits overwritten (replace in the map):\n```\n"
                 + "\n".join(overwritten) + "\n```\n")
    return text


def main():
    load_env(ENV_FILE)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("build", help="new build ID, e.g. 16321090")
    p.add_argument("--base-build", help=f"build the target branch currently has (first run only, "
                                        f"when it has no {BUILD_FILE})")
    p.add_argument("--repo-url", default=os.environ.get("GMS_REPO_URL"),
                   help="clone URL (env GMS_REPO_URL); only needed for the first clone")
    p.add_argument("--repo-dir", type=Path, default=DEFAULT_REPO_DIR,
                   help=f"working clone (default: {DEFAULT_REPO_DIR})")
    p.add_argument("-t", "--target", default="main", help="branch to open the PR against (default: main)")
    p.add_argument("--bitbucket-url", default=os.environ.get("BITBUCKET_URL"),
                   help="Bitbucket base URL (env BITBUCKET_URL); derived from an https remote if omitted")
    p.add_argument("-r", "--reviewer", action="append", default=[], help="reviewer user name; repeatable")
    p.add_argument("--no-pr", action="store_true", help="prepare the branch locally, don't push or open a PR")
    p.add_argument("--resume", action="store_true",
                   help="push and open the PR after resolving a cherry-pick conflict by hand")
    p.add_argument("--map", type=Path, default=DEFAULT_MAP,
                   help=f"repo path / zip path / strategy map (default: {DEFAULT_MAP})")
    p.add_argument("--downloads", type=Path, default=DEFAULT_DOWNLOADS,
                   help=f"where zips are kept (default: {DEFAULT_DOWNLOADS})")
    p.add_argument("--login", action="store_true", help="open the browser to sign in again")
    p.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help="browser profile dir")
    args = p.parse_args()

    token = os.environ.get("BITBUCKET_TOKEN")
    if not args.no_pr and not token:
        sys.exit("set BITBUCKET_TOKEN to a Bitbucket HTTP access token (or use --no-pr)")

    rules = load_map(args.map)
    branch = PR_BRANCH.format(build=args.build)
    git = prepare_clone(args.repo_url, args.repo_dir.expanduser(), args.target)
    origin_target = f"origin/{args.target}"
    recorded = (git.show(origin_target, BUILD_FILE) or "").strip()
    old_build = recorded or args.base_build
    if not old_build:
        sys.exit(f"{args.target} has no {BUILD_FILE}; pass --base-build with the build it was made from")
    if recorded and args.base_build and args.base_build != recorded:
        sys.exit(f"--base-build {args.base_build} disagrees with {BUILD_FILE} ({recorded})")
    if old_build == args.build:
        sys.exit(f"{args.target} is already on build {args.build}")
    old_tag, new_tag = (PRISTINE_TAG.format(build=b, map_hash=map_hash(rules))
                        for b in (old_build, args.build))

    if args.resume:
        if git("rev-parse", "--abbrev-ref", "HEAD") != branch:
            sys.exit(f"--resume: expected {branch} to be checked out in {git.repo}")
        if git.exists("CHERRY_PICK_HEAD") or git("diff", "--name-only", "--diff-filter=U"):
            sys.exit("--resume: the cherry-pick is still in progress; finish it with git cherry-pick --continue")
        for tag in (old_tag, new_tag):
            if not git.exists(f"refs/tags/{tag}"):
                sys.exit(f"--resume: {tag} not found; was {args.map} changed? Rerun without --resume")
        old_tree, new_tree = update_trees(git, old_tag, new_tag, old_build, args.build, bool(recorded))
    else:
        pristine_snapshot(git, old_build, args.downloads, args, rules)
        pristine_snapshot(git, args.build, args.downloads, args, rules)
        old_tree, new_tree = update_trees(git, old_tag, new_tag, old_build, args.build, bool(recorded))
        apply_update(git, rules, old_tree, new_tree, old_build, args.build, branch, args.target)

    print(f"Branch {branch} is ready in {git.repo}")
    if args.no_pr:
        return

    git("push", "origin", PRISTINE_BRANCH, f"refs/tags/{old_tag}", f"refs/tags/{new_tag}")
    git("push", "--force-with-lease", "origin", f"{branch}:refs/heads/{branch}")
    base, project, repo = bitbucket_coords(git("remote", "get-url", "origin"), args.bitbucket_url)
    url, created = create_pr(base, project, repo, token, branch, args.target,
                             f"Update gms_car_prod to build {args.build}",
                             describe(git, rules, old_tree, new_tree, old_build, args.build, args.target),
                             args.reviewer)
    print(f"{'Opened' if created else 'Updated existing'} pull request: {url}")


if __name__ == "__main__":
    main()
