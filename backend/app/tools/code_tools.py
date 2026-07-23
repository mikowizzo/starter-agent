"""Minimal file toolkit — read, write, edit, grep, glob, delete, move."""

import ast
import difflib
import os
import shutil
import subprocess
from pathlib import Path

from agno.tools import Toolkit

_EXCLUDE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".next"}
_EXCLUDE_GLOBS = [f"!**/{d}/**" for d in _EXCLUDE_DIRS] + [
    f"!**/{d}" for d in _EXCLUDE_DIRS
]


class CodeTools(Toolkit):
    """Read, write, edit, grep, glob, delete, move."""

    def __init__(self, base_dir: str | None = None) -> None:
        self.base = Path(base_dir or ".").resolve()
        super().__init__(
            name="code_tools",
            tools=[
                self.read, self.write, self.edit,
                self.grep, self.glob, self.delete, self.move,
            ],
        )

    # ── helpers ──────────────────────────────────────────────────────────

    def _safe_resolve(self, path: str) -> Path | str:
        """Resolve path, returning an ❌ string instead of raising."""
        try:
            p = (self.base / path).resolve()
            if not p.is_relative_to(self.base):
                raise ValueError(f"Path escapes base: {path}")
            return p
        except ValueError as e:
            return f"❌ {e}"

    @staticmethod
    def grep(*args: str) -> str:
        """Run ripgrep.

        Returns stdout on success (including rg's exit-1 "no matches"),
        or an ❌ error string.
        """
        try:
            proc = subprocess.run(
                ["rg", "--no-config", "--hidden", *args],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(self.base),
            )
        except FileNotFoundError:
            return "❌ rg not found"
        except subprocess.TimeoutExpired:
            return "❌ rg timeout"
        if proc.returncode not in (0, 1):
            return f"❌ rg error (exit {proc.returncode}): {proc.stderr[:200]}"
        return proc.stdout

    # ── read / write / edit ──────────────────────────────────────────────

    def read(self, path: str, offset: int = 0, limit: int = 500) -> str:
        """Read a file slice by line number. Output has 1-indexed line numbers.

        Args:
            path: Relative path to the file.
            offset: Zero-based line to start reading from.
            limit: Max lines to return (hard-capped at 2000).
        """
        p = self._safe_resolve(path)
        if isinstance(p, str):
            return p
        if not p.is_file():
            return f"❌ Not found: {p}"
        if offset < 0:
            return "❌ offset must be >= 0"
        if limit <= 0:
            return "❌ limit must be > 0"
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(lines)
        if total == 0:
            return f"⚠️ Empty file: {path}"
        if offset >= total:
            return (
                f"❌ offset {offset} is past end of file ({total} lines). Use offset=0."
            )
        slice_lines = lines[offset : offset + min(limit, 2000)]
        end = offset + len(slice_lines)
        width = len(str(end))
        numbered = [
            f"{offset + i + 1:>{width}}: {line}" for i, line in enumerate(slice_lines)
        ]
        out = f"[lines {offset + 1}\u2013{end} of {total} in {path}]\n" + "\n".join(
            numbered
        )
        if end < total:
            out += f"\n[read more with offset={end}]"
        return out

    def write(self, path: str, content: str) -> str:
        """Create or overwrite a file.

        Write to subdirectories rather than the workspace root to keep
        things organised.
        """
        p = self._safe_resolve(path)
        if isinstance(p, str):
            return p
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"✅ Wrote {content.count(chr(10)) + 1} lines to {p}"

    def edit(
        self,
        path: str,
        search: str = "",
        replace: str = "",
        *,
        old_start: int | None = None,
        old_end: int | None = None,
        replace_all: bool = False,
        dry_run: bool = False,
    ) -> str:
        """Edit a file by line range or exact string search.

        Provide EXACTLY ONE of:
          - old_start AND old_end (1-indexed, inclusive): replace that line range
            with `replace`. Preferred mode — pairs with read()'s line numbers.
          - search: replace matched text with `replace`. Must match exactly once
            unless replace_all=True.

        Args:
            path: File to edit.
            search: Text to find and replace (search mode).
            replace: Replacement text.
            old_start: First line number (1-indexed, line-range mode).
            old_end: Last line number (1-indexed, inclusive).
            replace_all: Allow >1 matches (search mode only).
            dry_run: Return diff without writing.
        Returns status + unified diff on success; ❌ on failure.
        Line-range edits report line-shift info for subsequent edits.
        Python files are syntax-checked before write.
        """
        p = self._safe_resolve(path)
        if isinstance(p, str):
            return p
        if not p.is_file():
            return f"❌ Not found: {p}"

        line_mode = old_start is not None or old_end is not None
        if line_mode and search:
            return "❌ Ambiguous: provide line range (old_start/old_end) OR search, not both."
        if not line_mode and not search:
            return "❌ Nothing to edit: provide either search or old_start/old_end."

        src_lines = p.read_text(encoding="utf-8", errors="replace").splitlines(
            keepends=True
        )

        if line_mode:
            if old_start is None or old_end is None:
                return "❌ Both old_start and old_end are required for line-range mode."
            if old_start < 1:
                return f"❌ old_start must be >= 1 (got {old_start})."
            if old_end < old_start:
                return f"❌ old_end ({old_end}) must be >= old_start ({old_start})."
            if old_end > len(src_lines):
                return f"❌ old_end ({old_end}) is past end of file ({len(src_lines)} lines)."

            replace_lines = replace.splitlines(keepends=True)
            if replace and not replace.endswith("\n"):
                replace_lines[-1] += "\n"
            new_lines = src_lines[: old_start - 1] + replace_lines + src_lines[old_end:]
        else:
            if not search.strip():
                return "❌ Empty search string."
            src = "".join(src_lines)
            count = src.count(search)
            if count == 0:
                return "❌ Search string not found."
            if count > 1 and not replace_all:
                return (
                    f"❌ Expected 1 match, found {count}. "
                    f"Use replace_all=True or provide old_start/old_end."
                )
            new_lines = src.replace(search, replace).splitlines(keepends=True)

        new_text = "".join(new_lines)
        if p.suffix.lower() == ".py":
            try:
                ast.parse(new_text)
            except SyntaxError as e:
                return f"❌ Would break syntax: {e.msg}"

        diff = self._make_diff(src_lines, new_lines, path)
        diff_lines = diff.splitlines()
        added = sum(1 for d in diff_lines if d.startswith("+") and not d.startswith("+++"))
        removed = sum(1 for d in diff_lines if d.startswith("-") and not d.startswith("---"))
        summary = f"✅ Edited {path} (+{added}/-{removed} lines)"

        # Report line-shift info for line-range edits (helps with consecutive edits)
        if line_mode and old_end is not None:
            delta = len(replace_lines) - (old_end - old_start + 1)
            if delta != 0:
                summary += f" [lines after {old_end} shifted by {delta:+d}]"

        if dry_run:
            return f"{summary} [dry-run]\n{diff}"
        p.write_text(new_text, encoding="utf-8")
        return f"{summary}\n{diff}"

    @staticmethod
    def _make_diff(old_lines: list[str], new_lines: list[str], path: str) -> str:
        """Compact unified diff without the ---/+++ headers."""
        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            lineterm="",
        )
        return "\n".join(
            line.rstrip() for line in diff if not line.startswith(("---", "+++"))
        )

    # ── grep / glob ──────────────────────────────────────────────────────

    def grep(
        self,
        pattern: str,
        path: str = ".",
        file_filter: str | None = None,
        ignore_case: bool = True,
    ) -> str:
        """Regex content search via ripgrep. Returns `file:line: content` lines.

        Args:
            pattern: Regex pattern.
            path: File or directory to search (default: workspace root).
            file_filter: Optional glob to filter types (e.g. "*.py").
            ignore_case: Case-insensitive search (default True).
        """
        if not pattern:
            return "❌ Empty pattern"
        root = self._safe_resolve(path)
        if isinstance(root, str):
            return root
        if not root.exists():
            return f"❌ Not found: {path}"
        args = ["-H", "--line-number"]
        if ignore_case:
            args.append("--ignore-case")
        if file_filter:
            args.extend(["-g", file_filter])
        for g in _EXCLUDE_GLOBS:
            args.extend(["-g", g])
        args += [pattern, str(root.relative_to(self.base))]
        out = self._rg(args)
        if out.startswith("❌"):
            return out
        lines = [ln for ln in out.splitlines() if ln]
        if not lines:
            return f"⚠️ No matches for '{pattern}'"
        if len(lines) > 50:
            return "\n".join(lines[:50]) + (
                f"\n\n⚠️ Showing first 50 of {len(lines)} matches — refine your pattern"
            )
        return "\n".join(lines)

    def glob(self, pattern: str, path: str = ".", ignore_case: bool = True) -> str:
        """Find files by glob pattern via ripgrep.
        Patterns without a "/" are auto-prefixed with "**/" for recursive search.

        Args:
            pattern: Glob pattern (e.g. "*.py", "src/**/*.tsx").
            path: Directory to search (default: workspace root).
            ignore_case: Case-insensitive matching (default True).
        """
        root = self._safe_resolve(path)
        if isinstance(root, str):
            return root
        if not root.exists():
            return f"❌ Not found: {path}"
        if not root.is_dir():
            return f"❌ Not a directory: {path}. Pass a directory path, not a file."
        if not pattern:
            return "❌ Empty pattern"
        if pattern.startswith("!"):
            return (
                "❌ Exclude globs (! prefix) are not supported — remove the leading '!'"
            )
        g = (
            f"**/{pattern}"
            if "/" not in pattern and not pattern.startswith("**/")
            else pattern
        )
        args = ["--files", "--sort", "path"]
        if ignore_case:
            args.append("--glob-case-insensitive")
        args.extend(["-g", g])
        for eg in _EXCLUDE_GLOBS:
            args.extend(["-g", eg])
        args.append(str(root.relative_to(self.base)))
        out = self._rg(args)
        if out.startswith("❌"):
            return out
        all_files = [str(self.base / f) for f in out.splitlines() if f]
        if not all_files:
            return f"⚠️ No files matched '{pattern}'"
        header = f"**{min(len(all_files), 200)} file(s)** matched `{pattern}`"
        if len(all_files) > 200:
            header += f" (showing first 200 of {len(all_files)})"
        return f"{header}\n\n" + "\n".join(all_files[:200])

    # ── delete / move ───────────────────────────────────────────────────

    def delete(self, path: str) -> str:
        """Delete a file or directory.

        Args:
            path: Relative path to the file or directory to delete.
        """
        p = self._safe_resolve(path)
        if isinstance(p, str):
            return p
        if p == self.base:
            return "❌ Cannot delete the base directory"
        if not p.exists():
            return f"❌ Not found: {path}"
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return f"✅ Deleted: {path}"

    def move(self, source: str, destination: str) -> str:
        """Move a file or directory. Refuses to overwrite existing files.

        Args:
            source: Relative path to the file or directory to move.
            destination: Relative path to the target location.
        """
        src_p = self._safe_resolve(source)
        if isinstance(src_p, str):
            return src_p
        dst_p = self._safe_resolve(destination)
        if isinstance(dst_p, str):
            return dst_p
        if not src_p.exists():
            return f"❌ Source not found: {source}"
        if dst_p.exists():
            return f"❌ Destination already exists (will not overwrite): {destination}"
        dst_p.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_p), str(dst_p))
        return f"✅ Moved: {source} → {destination}"
