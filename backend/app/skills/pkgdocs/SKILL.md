---
name: pkgdocs
description: >
  Search installed Python packages (site-packages) for source/doc files.
  Use when you need to grep or glob inside installed packages (e.g. agno)
  that the sandboxed read/grep/glob tools cannot reach. Works by importing
  the package in Python and walking its files from the shell.
license: MIT
---

# pkgdocs

The built-in read/grep/glob tools only see the workspace. Python can import
installed packages, so this script reaches their files via the shell.

## Usage

    python backend/app/skills/pkgdocs/pkgsearch.py session_name agno
    python backend/app/skills/pkgdocs/pkgsearch.py --names "session" agno
    python backend/app/skills/pkgdocs/pkgsearch.py --glob "*.md" "quickstart" agno

Flags: `--glob PAT` (default `*.py`), `--names` (match filenames), `-i` (ignore case).
Omit PACKAGE to search all of site-packages.
