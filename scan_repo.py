#!/usr/bin/env python3
"""Write an empty gms-map.txt listing what is in a directory of the repo.

Each file and directory under the path, subfolders included, gets a line with its repo path
and the strategy; the zip column holds the same path with its folder in <>,
ready to replace with <arch>/<path in zip>. Folders with no files directly in them
are skipped.
"""

import argparse
import subprocess
import sys
from pathlib import Path

from update_repo import BUILD_FILE, DEFAULT_MAP, STRATEGIES


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("path", type=Path, help="directory in a local checkout of the repo")
    p.add_argument("-s", "--strategy", choices=STRATEGIES, default="rebase",
                   help="strategy written on every line (default: rebase)")
    p.add_argument("-o", "--output", type=Path, default=DEFAULT_MAP,
                   help=f"map file to write (default: {DEFAULT_MAP}); - for stdout")
    p.add_argument("-f", "--force", action="store_true", help="overwrite an existing map file")
    args = p.parse_args()

    path = args.path.expanduser().resolve()
    if not path.is_dir():
        sys.exit(f"{args.path} is not a directory")
    to_stdout = str(args.output) == "-"
    if not to_stdout and args.output.exists() and not args.force:
        sys.exit(f"{args.output} exists; pass --force to overwrite it, or -o another file")

    res = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True)
    root = Path(res.stdout.strip()) if res.returncode == 0 else path

    files = [f for f in path.rglob("*") if f.is_file() and f.name != BUILD_FILE
             and ".git" not in f.relative_to(path).parts]
    # Only folders holding files directly; ones with just subfolders (or nothing) are skipped.
    dirs = {f.parent for f in files if f.parent != path}
    entries = sorted(f.relative_to(root).as_posix() + ("/" if f in dirs else "")
                     for f in [*files, *dirs])

    def placeholder(entry):
        """etc/gms/android.bp -> <etc/gms>/android.bp, for replacing the parent with the zip path."""
        parent, _, name = entry.rstrip("/").rpartition("/")
        return f"<{parent}>/{name}" + ("/" if entry.endswith("/") else "")

    header = DEFAULT_MAP.read_text().split("\n\n", 1)[0] if DEFAULT_MAP.exists() else ""
    width = max((len(e) for e in entries), default=0)
    zwidth = max((len(placeholder(e)) for e in entries), default=0)
    out = [header.rstrip(), ""] + [f"{e:<{width}}  {placeholder(e):<{zwidth}}  {args.strategy}"
                                   for e in entries]
    text = "\n".join(out) + "\n"
    if to_stdout:
        sys.stdout.write(text)
    else:
        args.output.write_text(text)
        print(f"{len(entries)} entries; wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
