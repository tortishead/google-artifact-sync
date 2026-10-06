#!/usr/bin/env python3
"""Write an initial gms-map.txt by finding where the files of a build's zips live in the repo.

Give it the build the target branch was made from. Each zip file is matched to a
repo file with the same content (git blob or Git LFS object), or, failing that,
with the longest common path ending (a file edited locally). The matches are then
folded into as few directory lines as possible, with single-file lines for the
exceptions. Zip files with no counterpart in the repo are listed at the end of
the map as comments, so update_repo.py refuses to run until each one is placed.

Review the result before using it: check the strategies and the unplaced files.
"""

import argparse
import hashlib
import os
import subprocess
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

from fetch_artifact import ARCHES, DEFAULT_PROFILE, AuthRequired, fetch_builds
from update_repo import (BUILD_FILE, DEFAULT_DOWNLOADS, DEFAULT_MAP, DEFAULT_REPO_DIR, ENV_FILE,
                         STRATEGIES, load_env, prepare_clone)

LFS_POINTER_MAX = 1024


def repo_index(git, rev):
    """{content key: [repo paths]} and {basename: [repo paths]} for the files at rev.

    Content keys are ("git", blob sha1), or ("lfs", sha256) for Git LFS pointers.
    """
    by_content, by_name, small = defaultdict(list), defaultdict(list), {}
    for entry in filter(None, git("ls-tree", "-r", "-l", "-z", rev).split("\0")):
        meta, path = entry.split("\t", 1)
        mode, kind, sha, size = meta.split()
        if kind != "blob" or path == BUILD_FILE:
            continue
        by_content["git", sha].append(path)
        by_name[path.rsplit("/", 1)[-1]].append(path)
        if size.isdigit() and int(size) <= LFS_POINTER_MAX:
            small[sha] = path

    if small:
        out = subprocess.run(["git", "-C", str(git.repo), "cat-file", "--batch"],
                             input="".join(f"{sha}\n" for sha in small).encode(),
                             stdout=subprocess.PIPE, check=True).stdout
        pos = 0
        for _ in small:
            header_end = out.index(b"\n", pos)
            sha, _, size = out[pos:header_end].decode().split()
            body = out[header_end + 1:header_end + 1 + int(size)]
            pos = header_end + 1 + int(size) + 1
            if body.startswith(b"version https://git-lfs"):
                for line in body.decode(errors="replace").splitlines():
                    if line.startswith("oid sha256:"):
                        by_content["lfs", line.split(":", 1)[1]].append(small[sha])
    return by_content, by_name


def zip_entries(zip_path):
    """[(name, (git blob sha1, sha256))] for the files in the zip."""
    entries = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            blob = hashlib.sha1(b"blob %d\0" % info.file_size)
            plain = hashlib.sha256()
            with zf.open(info) as f:
                while chunk := f.read(1 << 20):
                    blob.update(chunk)
                    plain.update(chunk)
            entries.append((info.filename, (blob.hexdigest(), plain.hexdigest())))
    return entries


def split_pair(repo_path, zip_path):
    """(repo prefix, zip prefix, length of the shared path ending in parts)."""
    r, z = repo_path.split("/"), zip_path.split("/")
    n = 0
    while n < min(len(r), len(z)) and r[-1 - n] == z[-1 - n]:
        n += 1
    rp, zp = "/".join(r[:len(r) - n]), "/".join(z[:len(z) - n])
    return (rp + "/" if rp else ""), (zp + "/" if zp else ""), n


def match(entries_by_arch, by_content, by_name):
    """{(arch, zip name): repo path} for every zip file that has a counterpart in the repo."""
    def candidates(name, hashes):
        """{repo path: same content?} for the repo files that could be this zip file."""
        same = by_content.get(("git", hashes[0]), []) + by_content.get(("lfs", hashes[1]), [])
        # A name alone is weak evidence: also require the parent directory to agree.
        similar = [p for p in by_name.get(name.rsplit("/", 1)[-1], []) if split_pair(p, name)[2] >= 2]
        return {**dict.fromkeys(similar, False), **dict.fromkeys(same, True)}

    # Prefix pairs (repo prefix, zip prefix) of the content matches, per arch. They
    # pick between candidates, e.g. for a file identical in both arches' zips (and
    # so in both repo trees), or one edited locally whose original is in the other.
    votes = {arch: Counter() for arch in entries_by_arch}
    for arch, entries in entries_by_arch.items():
        for name, hashes in entries:
            same = [p for p, s in candidates(name, hashes).items() if s]
            best = max((split_pair(p, name)[2] for p in same), default=0)
            for p in same:
                rp, zp, n = split_pair(p, name)
                if n == best:
                    votes[arch][rp, zp] += 1

    found, claimed = {}, set()
    for arch, entries in entries_by_arch.items():
        for name, hashes in entries:
            paths = candidates(name, hashes)
            def score(p):
                rp, zp, n = split_pair(p, name)
                return votes[arch][rp, zp], paths[p], n
            for p in sorted(paths, key=score, reverse=True):
                if p not in claimed:
                    found[arch, name] = p
                    claimed.add(p)
                    break
    return found


def fold(arch, names, found):
    """Map lines [(repo path, zip path)] for one arch that place every matched file.

    Walks the zip's directory tree. At each directory, a directory line is added
    when more of the files under it agree on one than on the line inherited from
    above; files the line in force misplaces get a line of their own.
    """
    tree = {}
    for name in names:
        node = tree
        for part in name.split("/")[:-1]:
            node = node.setdefault(part + "/", {})
        node.setdefault("", []).append(name)

    def files_under(node):
        yield from node.get("", [])
        for key, child in node.items():
            if key:
                yield from files_under(child)

    def places(rule, name):
        return (rule is not None and name.startswith(rule[1])
                and rule[0] + name[len(rule[1]):] == found.get((arch, name)))

    lines = []

    def walk(node, zip_dir, rule):
        if zip_dir:  # directory lines need a zip directory, not the whole zip
            options = Counter()
            for name in files_under(node):
                if (arch, name) in found:
                    rp, zp, _ = split_pair(found[arch, name], name)
                    if zip_dir.startswith(zp):
                        options[rp + zip_dir[len(zp):], zip_dir] += 1
            if options:
                best, count = options.most_common(1)[0]
                inherited = sum(places(rule, n) for n in files_under(node))
                if best != rule and count > inherited:
                    lines.append(best)
                    rule = best
        for name in node.get("", []):
            if (arch, name) in found and not places(rule, name):
                lines.append((found[arch, name], name))
        for key, child in sorted(node.items()):
            if key:
                walk(child, zip_dir + key, rule)

    walk(tree, "", None)
    return lines


def unplaced(arch, names, lines):
    """Zip files no line covers (longest zip path wins, as in update_repo.py)."""
    def covered(name):
        return any(name.startswith(z) if z.endswith("/") else name == z for _, z in lines)
    return [n for n in names if not covered(n)]


def render(build, rev, lines_by_arch, missing, strategy, local_only):
    header = DEFAULT_MAP.read_text().split("\n\n", 1)[0] if DEFAULT_MAP.exists() else ""
    out = [header.rstrip(), "",
           f"# Generated by scan_repo.py from build {build} and {rev}.", ""]
    rows = sorted((r, f"{arch}/{z}", strategy) for arch, lines in lines_by_arch.items() for r, z in lines)
    width = max((len(r) for r, _, _ in rows), default=0)
    zwidth = max((len(z) for _, z, _ in rows), default=0)
    for r, z, s in rows:
        out.append(f"{r:<{width}}  {z:<{zwidth}}  {s}".rstrip())
    if missing:
        out += ["", f"# Not found in the repo ({len(missing)} files). Map each one (or its"
                    " directory) and remove it from this list:"]
        out += [f"#   {m}" for m in missing]
    if local_only:
        out += ["", f"# {len(local_only)} repo files under the mapped directories are not in the"
                    " zips; they are kept as local files, e.g.:"]
        out += [f"#   {p}" for p in local_only[:20]]
    return "\n".join(out) + "\n"


def main():
    load_env(ENV_FILE)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("build", help="build the target branch was made from, e.g. 16321090")
    p.add_argument("-a", "--arch", action="append", choices=ARCHES,
                   help="arch to scan; repeatable (default: all)")
    p.add_argument("--repo-url", default=os.environ.get("GMS_REPO_URL"),
                   help="clone URL (env GMS_REPO_URL); only needed for the first clone")
    p.add_argument("--repo-dir", type=Path, default=DEFAULT_REPO_DIR,
                   help=f"working clone (default: {DEFAULT_REPO_DIR})")
    p.add_argument("-t", "--target", default="main", help="branch to scan (default: main)")
    p.add_argument("-s", "--strategy", choices=STRATEGIES, default="rebase",
                   help="strategy written on every line (default: rebase)")
    p.add_argument("-o", "--output", type=Path, default=DEFAULT_MAP,
                   help=f"map file to write (default: {DEFAULT_MAP}); - for stdout")
    p.add_argument("-f", "--force", action="store_true", help="overwrite an existing map file")
    p.add_argument("--downloads", type=Path, default=DEFAULT_DOWNLOADS,
                   help=f"where zips are kept (default: {DEFAULT_DOWNLOADS})")
    p.add_argument("--login", action="store_true", help="open the browser to sign in again")
    p.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help="browser profile dir")
    args = p.parse_args()

    to_stdout = str(args.output) == "-"
    if not to_stdout and args.output.exists() and not args.force:
        sys.exit(f"{args.output} exists; pass --force to overwrite it, or -o another file")

    arches = args.arch or list(ARCHES)
    git = prepare_clone(args.repo_url, args.repo_dir.expanduser(), args.target)
    rev = f"origin/{args.target}"
    try:
        zips, failed = fetch_builds([args.build], arches, args.downloads, args.profile, args.login)
    except AuthRequired as e:
        sys.exit(f"Login failed: {e}")
    if failed:
        sys.exit(f"could not download build {args.build}")

    print(f"Indexing {rev}", file=sys.stderr)
    by_content, by_name = repo_index(git, rev)
    entries = {}
    for arch in arches:
        print(f"Hashing {zips[args.build, arch].name}", file=sys.stderr)
        entries[arch] = zip_entries(zips[args.build, arch])
    found = match(entries, by_content, by_name)

    lines_by_arch, missing = {}, []
    for arch in arches:
        names = [n for n, _ in entries[arch]]
        lines_by_arch[arch] = fold(arch, names, found)
        missing += [f"{arch}/{n}" for n in unplaced(arch, names, lines_by_arch[arch])]

    dirs = [r for lines in lines_by_arch.values() for r, _ in lines if r.endswith("/")]
    local_only = sorted(set(path for paths in by_name.values() for path in paths
                            if any(path.startswith(d) for d in dirs)) - set(found.values()))

    text = render(args.build, rev, lines_by_arch, missing, args.strategy, local_only)
    if to_stdout:
        sys.stdout.write(text)
    else:
        args.output.write_text(text)
    total = sum(len(e) for e in entries.values())
    print(f"{len(found)} of {total} zip files found in the repo, "
          f"{sum(map(len, lines_by_arch.values()))} map lines, {len(missing)} not placed"
          + ("" if to_stdout else f"; wrote {args.output}"), file=sys.stderr)


if __name__ == "__main__":
    main()
