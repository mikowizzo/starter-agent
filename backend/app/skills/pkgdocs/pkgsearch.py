#!/usr/bin/env python3
"""Grep/glob inside installed Python packages (paths are sandboxed, Python isn't).

Usage:
  python pkgsearch.py PATTERN [PACKAGE]                  # grep contents (default *.py)
  python pkgsearch.py PATTERN [PACKAGE] --glob "*.md"    # grep other files
  python pkgsearch.py --names PATTERN [PACKAGE]          # match filenames instead
  PACKAGE omitted -> search all of site-packages.
"""
import argparse
import fnmatch
import importlib.util
import os
import re
import site
import sys


def roots(pkg: str | None) -> list[str]:
    if pkg:
        spec = importlib.util.find_spec(pkg)
        if spec is None:
            sys.exit(f"package {pkg!r} not found")
        loc = spec.submodule_search_locations
        return [list(loc)[0]] if loc else [os.path.dirname(spec.origin)]
    return site.getsitepackages()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pattern")
    ap.add_argument("pkg", nargs="?")
    ap.add_argument("--glob", default="*.py", help="file pattern to search (default *.py)")
    ap.add_argument("--names", action="store_true", help="match filenames, not contents")
    ap.add_argument("-i", "--ignore-case", action="store_true")
    a = ap.parse_args()

    pat = re.compile(a.pattern, re.IGNORECASE if a.ignore_case else 0)
    hits = 0
    for root in roots(a.pkg):
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d not in ("__pycache__", ".git", "node_modules", "dist")]
            for fn in fns:
                if not fnmatch.fnmatch(fn, a.glob):
                    continue
                path = os.path.join(dp, fn)
                if a.names:
                    if pat.search(fn):
                        print(path)
                        hits += 1
                    continue
                try:
                    with open(path, encoding="utf-8", errors="ignore") as fh:
                        for i, line in enumerate(fh, 1):
                            if pat.search(line):
                                print(f"{path}:{i}: {line.rstrip()[:300]}")
                                hits += 1
                except OSError:
                    pass
    print(f"--- {hits} hit(s) ---", file=sys.stderr)


if __name__ == "__main__":
    main()
