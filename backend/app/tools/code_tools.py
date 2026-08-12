"""Minimal file toolkit — read, write, edit, grep, glob, delete, move, shell."""

import ast
import difflib
import fcntl
import functools
import hashlib
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agno.tools import Toolkit

_EXCLUDE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".next"}
_EXCLUDE_GLOBS = [f"!**/{d}/**" for d in _EXCLUDE_DIRS] + [
    f"!**/{d}" for d in _EXCLUDE_DIRS
]

_EDIT_LOCK_FILE = os.path.join(tempfile.gettempdir(), "starter-code-edit.lock")
_EDIT_THREAD_LOCK = threading.Lock()

# ── shell tool guardrails ─────────────────────────────────────────────
_SHELL_TIMEOUT = 30
_SHELL_MAX_OUTPUT = 8000
# Commands that reach past the workspace (privilege escalation, secret
# dumps, host-level destruction). Never whitelist these.
_SHELL_BLOCKED_CMDS = {
    "sudo", "su", "env", "printenv", "shutdown", "reboot", "poweroff",
    "mkfs", "dd", "fdisk", "iptables", "killall",
}


@contextmanager
def _edit_lock():
    """Serialize file mutations across threads and processes.

    edit/write each do a whole-file read-modify-write. When the model fires
    several of them in parallel, their writes clobber each other and edits
    silently vanish. Every mutator runs under this lock, so parallel calls
    execute one at a time and each sees the latest file. The flock is
    released by the kernel if the process dies (no stale locks).
    """
    with _EDIT_THREAD_LOCK:  # serialize same-process threads
        fd = os.open(_EDIT_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)  # serialize across processes too
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _serialized(fn):
    """Wrap a mutating tool so its whole read-modify-write runs under the lock."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _edit_lock():
            return fn(*args, **kwargs)

    return wrapper


class CodeTools(Toolkit):
    """Read, write, edit, grep, glob, delete, move."""

    def __init__(self, base_dir: str | None = None) -> None:
        self.base = Path(base_dir or ".").resolve()
        # Mutators run under the edit lock: parallel calls would otherwise
        # clobber each other's whole-file read-modify-write (lost updates).
        self.write = _serialized(self.write)
        self.delete = _serialized(self.delete)
        self.move = _serialized(self.move)
        super().__init__(
            name="code_tools",
            tools=[
                self.read, self.write, self.edit,
                self.grep, self.glob, self.delete, self.move, self.shell,
            ],
        )

    # ── helpers ──────────────────────────────────────────────────────────

    #: Names that must never be read/written/edited/deleted at any depth.
    _DENIED_NAMES: frozenset[str] = frozenset({".git", ".env"})

    def _safe_resolve(self, path: str) -> Path | str:
        """Resolve path, returning an ❌ string instead of raising."""
        try:
            p = (self.base / path).resolve()
            if not p.is_relative_to(self.base):
                raise ValueError(f"Path escapes base: {path}")
            for part in p.relative_to(self.base).parts:
                if part in self._DENIED_NAMES:
                    raise ValueError(f"Access to {part!r} is blocked")
            return p
        except ValueError as e:
            return f"❌ {e}"

    def _rg(self, args: list[str]) -> str:
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

    _HASH_LEN = 16  # truncated sha256; plenty for optimistic concurrency

    #: Unicode confusables → ASCII for MATCHING ONLY (never written to disk).
    #: Covers smart quotes, dashes, hyphens, spaces, invisible chars, and
    #: bidi controls (Trojan Source, CVE-2021-42574).
    _ASCII_TRANSLATION = str.maketrans({
        # Smart quotes
        "\u2018": "'",  # left single quote
        "\u2019": "'",  # right single quote / curly apostrophe
        "\u201a": "'",  # single low-9 quote
        "\u201c": '"',  # left double quote
        "\u201d": '"',  # right double quote
        "\u201e": '"',  # double low-9 quote
        # Primes & acutes
        "\u2032": "'",  # prime
        "\u2033": '"',  # double prime
        "\u2035": "`",  # reversed prime
        "\u00b4": "'",  # acute accent
        "\u02bc": "'",  # modifier letter apostrophe
        # Dashes & hyphens
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u2212": "-",  # minus sign
        "\u2010": "-",  # hyphen
        "\u2011": "-",  # non-breaking hyphen
        # Ellipsis
        "\u2026": "...",  # horizontal ellipsis
        # Spaces → regular space
        "\u00a0": " ",  # non-breaking space
        "\u2002": " ",  # en space
        "\u2003": " ",  # em space
        "\u2004": " ",  # three-per-em space
        "\u2005": " ",  # four-per-em space
        "\u2006": " ",  # six-per-em space
        "\u2007": " ",  # figure space
        "\u2008": " ",  # punctuation space
        "\u2009": " ",  # thin space
        "\u200a": " ",  # hair space
        "\u202f": " ",  # narrow no-break space
        "\u205f": " ",  # medium mathematical space
        "\u3000": " ",  # ideographic space (CJK)
        # Invisible / zero-width → stripped
        "\u200b": "",   # zero-width space
        "\u200c": "",   # zero-width non-joiner
        "\u200d": "",   # zero-width joiner
        "\u2060": "",   # word joiner
        "\u00ad": "",   # soft hyphen
        "\ufeff": "",   # BOM / zero-width no-break space
        # Bidi controls (Trojan Source, CVE-2021-42574) → stripped
        "\u202a": "", "\u202b": "", "\u202c": "", "\u202d": "", "\u202e": "",
        "\u2066": "", "\u2067": "", "\u2068": "", "\u2069": "",
    })

    @classmethod
    def _normalize_line(cls, line: str) -> str:
        """Normalize a single line for matching/hashing (never written to disk)."""
        return line.translate(cls._ASCII_TRANSLATION).rstrip()

    @classmethod
    def _normalize_lines(cls, text: str) -> list[str]:
        """Canonical form for matching and hashing.

        CRLF/CR → LF, Unicode confusables → ASCII, rstrip each line
        (leading whitespace preserved).  Used ONLY for matching and hashing
        — never for display or writing.
        """
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return [cls._normalize_line(line) for line in text.split("\n")]

    @staticmethod
    def _split_lines(text: str) -> list[str]:
        """Split text into lines with CRLF/CR normalised to LF.

        Preserves all content (Unicode, trailing whitespace) — use for raw
        lines that will be written back to disk.
        """
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return text.split("\n")

    @classmethod
    def _content_hash(cls, lines: list[str]) -> str:
        """Hash of the *normalized* content, so read() and edit() always agree."""
        digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
        return digest[: cls._HASH_LEN]

    def _read_lines(self, path: str) -> tuple[Path, list[str], list[str]] | str:
        """Resolve + read file. Returns (path, raw_lines, norm_lines) or error.

        raw_lines preserve original content for writing back to disk.
        norm_lines are used for matching and hashing.
        The two lists are parallel: raw_lines[i] corresponds to norm_lines[i].
        """
        resolved = self._safe_resolve(path)
        if isinstance(resolved, str):
            return resolved
        if not resolved.exists():
            return f"❌ Not found: {path}"
        if not resolved.is_file():
            return f"❌ Not a regular file: {path}"
        try:
            text = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"❌ Cannot decode {path} as UTF-8 (binary file?)"
        except OSError as exc:
            return f"❌ Cannot read {path}: {exc}"
        # NUL byte check — likely binary
        if "\x00" in text[:8192]:
            return f"❌ Binary file (NUL byte detected): {path}"
        raw_lines = self._split_lines(text)
        norm_lines = [self._normalize_line(l) for l in raw_lines]
        return resolved, raw_lines, norm_lines

    @staticmethod
    def _find_occurrences(haystack: list[str], needle: list[str]) -> list[int]:
        """0-indexed start lines of every occurrence of needle in haystack."""
        n = len(needle)
        if n == 0 or n > len(haystack):
            return []
        first = needle[0]
        limit = len(haystack) - n
        return [
            i
            for i in range(limit + 1)
            if haystack[i] == first and haystack[i : i + n] == needle
        ]

    @staticmethod
    def _closest_match(
        haystack: list[str], needle: list[str], cutoff: float = 0.55
    ) -> tuple[float, int, list[str]] | None:
        """Sliding-window fuzzy match; returns (ratio, start_idx, window) or None."""
        n = len(needle)
        if n == 0 or not haystack:
            return None
        if len(haystack) < n:
            windows: list[tuple[int, list[str]]] = [(0, haystack)]
        else:
            windows = [(i, haystack[i : i + n]) for i in range(len(haystack) - n + 1)]
        best_ratio, best = 0.0, None
        for i, window in windows:
            sm = difflib.SequenceMatcher(None, needle, window, autojunk=False)
            if sm.real_quick_ratio() < best_ratio or sm.quick_ratio() < best_ratio:
                continue
            ratio = sm.ratio()
            if ratio > best_ratio:
                best_ratio, best = ratio, (i, window)
        if best is None or best_ratio < cutoff:
            return None
        return best_ratio, best[0], best[1]

    def _not_found_error(
        self,
        path: str,
        lines: list[str],
        old_lines: list[str],
        new_lines: list[str],
        prefix: str,
    ) -> str:
        parts = [f"❌ {prefix}old_string not found in {path}."]
        # Idempotency hint: is this edit already applied?
        already = self._find_occurrences(lines, new_lines)
        if already:
            where = ", ".join(str(i + 1) for i in already[:5])
            parts.append(
                f"HINT: new_string is already present at line(s) {where} — this edit "
                "appears to have ALREADY BEEN APPLIED. Verify with read() before retrying."
            )
        # Near-match for self-correction
        near = self._closest_match(lines, old_lines)
        if near is not None:
            ratio, start, window = near
            parts.append(
                f"Closest near-match ({ratio:.0%} similar) at line {start + 1} "
                "(expected [-] vs actual [+]):"
            )
            parts.append(self._make_diff(old_lines, window, f"{path}@line{start + 1}"))
            parts.append(
                "Re-read the file and adjust old_string to match exactly "
                "(indentation matters; trailing whitespace is ignored)."
            )
        elif not already:
            parts.append(
                "No similar region found — the target code has likely changed. "
                "Re-read the file for its current contents."
            )
        return "\n".join(parts)

    def _apply_edit(
        self,
        path: str,
        raw_lines: list[str],
        norm_lines: list[str],
        old_string: str,
        new_string: str,
        replace_all: bool,
        index: int,
    ) -> str | None:
        """Apply one edit to raw_lines/norm_lines in place (parallel mutation).

        Matching uses norm_lines; splicing uses raw_lines. Both arrays are
        mutated in lockstep to remain parallel. new_string is inserted raw
        (never normalized) so Unicode content is preserved on write.
        """
        prefix = f"edit[{index}]: "
        old_norm = self._normalize_lines(old_string)
        new_norm = self._normalize_lines(new_string)

        if old_norm == new_norm:
            return f"❌ {prefix}old_string and new_string are identical — no-op edit."

        hits = self._find_occurrences(norm_lines, old_norm)
        if not hits:
            return self._not_found_error(
                path, norm_lines, old_norm, new_norm, prefix
            )

        # new_string lines: raw for splicing into raw_lines, normalized for
        # keeping norm_lines parallel.
        new_raw = self._split_lines(new_string)

        if replace_all:
            for start in reversed(hits):
                raw_lines[start : start + len(old_norm)] = new_raw
                norm_lines[start : start + len(old_norm)] = new_norm
            return None

        if len(hits) > 1:
            where = ", ".join(str(h + 1) for h in hits[:10])
            return (
                f"❌ {prefix}old_string matches {len(hits)} locations (lines {where}) "
                "but replace_all is false. Include more context or set replace_all=true."
            )

        start = hits[0]
        raw_lines[start : start + len(old_norm)] = new_raw
        norm_lines[start : start + len(old_norm)] = new_norm
        return None

    def read(self, path: str, offset: int = 0, limit: int = 500) -> str:
        """Read a file slice by line number. Output has 1-indexed line numbers.

        The header includes a content hash. Pass that hash as `expected_hash`
        to `edit()` for optimistic concurrency — if the file changed since you
        read it, the edit is refused.

        Args:
            path: Relative path to the file.
            offset: Zero-based line to start reading from.
            limit: Max lines to return (hard-capped at 2000).
        """
        result = self._read_lines(path)
        if isinstance(result, str):
            return result
        _resolved, raw_lines, norm_lines = result
        total = len(raw_lines)
        file_hash = self._content_hash(norm_lines)

        if total == 0 or (total == 1 and raw_lines[0] == ""):
            return f"⚠️ Empty file: {path}"
        if offset < 0:
            return "❌ offset must be >= 0"
        if offset >= total:
            return f"❌ offset {offset} is past end of file ({total} lines). Use offset=0."
        if limit <= 0:
            return "❌ limit must be > 0"

        slice_lines = raw_lines[offset : offset + min(limit, 2000)]
        end = offset + len(slice_lines)
        width = len(str(end))
        numbered = [
            f"{offset + i + 1:>{width}}: {line}" for i, line in enumerate(slice_lines)
        ]
        out = (
            f"[lines {offset + 1}–{end} of {total} in {path} | hash={file_hash}]\n"
            + "\n".join(numbered)
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

    @_serialized
    def edit(
        self,
        path: str,
        edits: list[dict[str, Any]] | None = None,
        expected_hash: str | None = None,
    ) -> str:
        """Apply one or more search-and-replace edits to a file, atomically.

        Each edit replaces an exact block of text (old_string) with new text
        (new_string). Edits are applied in order — each edit sees the results
        of the previous ones. The batch is atomic: if ANY edit fails, NOTHING
        is written to disk.

        Matching rules:
          - old_string must match the file content exactly, EXCEPT that trailing
            whitespace per line, CRLF-vs-LF differences, and common Unicode
            confusables (smart quotes, em/en dashes, non-breaking spaces, etc.)
            are normalized to ASCII (leading whitespace / indentation must match
            exactly).
          - By default old_string must match exactly ONE location; if it matches
            several, add more surrounding context or set replace_all to true.

        For Python (.py) files, the result is syntax-checked before writing.

        Args:
            path: File to edit, relative to the workspace root.
            edits: List of edit objects, each with keys:
                - "old_string" (str, required): text to find.
                - "new_string" (str, required): replacement text.
                - "replace_all" (bool, optional, default false): replace all
                  occurrences instead of requiring a unique match.
                Example: [{"old_string": "def f():\\n    pass",
                           "new_string": "def f():\\n    return 1"}]
            expected_hash: Optional hash from a prior read()/edit() call. If
                provided and the file's current hash differs, the edit is
                rejected (re-read the file and retry).

        Returns:
            On success: '✅' summary with old/new hashes + unified diff.
            On failure: '❌' error with details for self-correction.
        """
        if not edits:
            return "❌ edits list is empty — provide at least one edit."
        for i, e in enumerate(edits):
            if not isinstance(e, dict):
                return f"❌ edits[{i}] must be an object with old_string/new_string."
            old, new = e.get("old_string"), e.get("new_string")
            if not isinstance(old, str) or not isinstance(new, str):
                return (
                    f"❌ edits[{i}] requires string fields 'old_string' and 'new_string'."
                )
            if old == "":
                return f"❌ edits[{i}]: old_string must not be empty."
            if not isinstance(e.get("replace_all", False), bool):
                return f"❌ edits[{i}]: 'replace_all' must be a boolean."

        result = self._read_lines(path)
        if isinstance(result, str):
            return result
        resolved, raw_original, norm_original = result

        current_hash = self._content_hash(norm_original)
        if expected_hash is not None and expected_hash != current_hash:
            return (
                f"❌ Hash mismatch for {path}: expected {expected_hash}, "
                f"current {current_hash}. The file changed — re-read and retry."
            )

        # Parallel working copies: raw (for writing) and norm (for matching).
        raw_working = list(raw_original)
        norm_working = list(norm_original)
        for i, e in enumerate(edits):
            err = self._apply_edit(
                path,
                raw_working,
                norm_working,
                e["old_string"],
                e["new_string"],
                e.get("replace_all", False),
                i,
            )
            if err is not None:
                return err

        if raw_working == raw_original:
            return f"❌ No changes to {path} — all edits were no-ops."

        new_text = "\n".join(raw_working)
        if resolved.suffix.lower() == ".py":
            try:
                ast.parse(new_text)
            except SyntaxError as exc:
                return (
                    f"❌ Edit rejected — result is not valid Python: {exc.msg} "
                    f"(line {exc.lineno}). No changes were written."
                )

        try:
            resolved.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            return f"❌ Failed to write {path}: {exc}"

        new_hash = self._content_hash(norm_working)
        diff = self._make_diff(raw_original, raw_working, path)
        return (
            f"✅ Applied {len(edits)} edit(s) to {path} "
            f"({len(raw_original)} → {len(raw_working)} lines, "
            f"hash {current_hash} → {new_hash})\n{diff}"
        )

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
        args += ["-e", pattern, "--", str(root.relative_to(self.base))]
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
        args += ["--", str(root.relative_to(self.base))]
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
        if os.path.lexists(dst_p):
            return f"❌ Destination already exists (will not overwrite): {destination}"
        if src_p == dst_p:
            return "❌ Source and destination are the same path."
        if src_p in dst_p.parents:
            return "❌ Cannot move a directory into itself."
        dst_p.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src_p), str(dst_p))
        except (OSError, shutil.Error) as e:
            return f"❌ Move failed: {e}"
        return f"✅ Moved: {source} → {destination}"

    # ── shell (guarded) ────────────────────────────────────────────────

    def _shell_guard(self, command: str) -> str | None:
        """Return an error string if the command escapes the workspace, else None.

        Uses shlex.split for proper shell tokenisation (handles quoting
        correctly) then applies three checks:
          1. Blocked commands (sudo, env, dd, mkfs, ...)
          2. Home-dir escapes (~/, $HOME)
          3. Path containment — resolve then is_relative_to

        Interpreter code arguments (after python3 -c, node -e, etc.) are
        exempted by position — they're code, not filesystem paths.

        ponytail: regex guardrail, not a sandbox — command substitution
        ($(cat /etc/passwd)) can dodge it. Real containment = run in a
        container/bwrap. Add that only if this leaks.
        """
        if self._env_access(command):
            return "❌ Shell: .env access is blocked."

        # Proper shell tokenisation — fails closed on unbalanced quotes.
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            return ("❌ Shell: unparseable command "
                    "(unbalanced quoting). Rewrite and retry.")

        # Interpreters where the argument after -c / -e is code, not a path.
        _CODE_FLAGS = {"-c", "-e"}
        _INTERPRETERS = {"python", "python3", "node", "ruby", "perl"}

        prev = None
        for tok in tokens:
            # Skip interpreter code arguments (python3 -c "...", node -e "...")
            if prev in _INTERPRETERS and tok in _CODE_FLAGS:
                prev = tok
                continue
            if prev in _CODE_FLAGS:
                prev = tok
                continue

            if tok in _SHELL_BLOCKED_CMDS or tok.startswith("mkfs"):
                return f"❌ Shell: '{tok}' is blocked."
            if tok.startswith(("~", "$HOME", "${HOME}")):
                return f"❌ Shell: path escapes workspace: {tok}"
            # Check the token and any value after = (--flag=/abs/path)
            # so long-flag form can't smuggle an absolute path.
            for part in re.split(r"=", tok, maxsplit=1):
                if "/" in part or part.startswith("."):
                    try:
                        resolved = (self.base / part).resolve()
                        if not resolved.is_relative_to(self.base):
                            return f"❌ Shell: path outside workspace: {tok}"
                    except (ValueError, OSError):
                        return f"❌ Shell: unresolvable path: {tok}"
            prev = tok
        return None

    @staticmethod
    def _env_access(command: str) -> bool:
        """True if the command references a .env* file path (bare or quoted).

        shlex.split strips quotes, so `cat ".env"` -> token `.env`. A token
        is a path if it is `.env`, `.env.<ext>` (incl. globs like `.env*`),
        or `.../.env[...]`. Prose like `.env is blocked` never matches, so
        commit messages and docs aren't false positives.
        """
        try:
            tokens = shlex.split(command)
        except ValueError:  # unbalanced quotes — fall back to raw tokens
            tokens = re.findall(r"\S+", command)
        return any(
            re.search(r"(?:^|/)\.env(?:\.[A-Za-z0-9_]+)?\*?$", tok)
            for tok in tokens
        )

    def shell(self, command: str) -> str:
        """Run a shell command, cwd = workspace root.

        Guardrails (best-effort, not a sandbox):
          - .env access blocked (also env/printenv)
          - paths outside the workspace blocked (.., ~, $HOME, absolutes)
          - destructive/system commands blocked (sudo, su, dd, mkfs, ...)
          - interpreter code args (python3 -c, node -e) exempt from path checks
          - unparsable quoting fails closed (rewrite and retry)

        Args:
            command: Shell command to run.
        """
        if not command.strip():
            return "❌ Shell: empty command"
        err = self._shell_guard(command)
        if err:
            return err
        proc = subprocess.Popen(
            command, shell=True, cwd=str(self.base),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            errors="replace",
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=_SHELL_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = proc.communicate()
            return f"❌ Shell: timed out after {_SHELL_TIMEOUT}s — killed: {err.strip()[:300]}"
        text = out + (("\n[stderr]\n" + err) if err.strip() else "")
        if len(text) > _SHELL_MAX_OUTPUT:
            text = text[:_SHELL_MAX_OUTPUT] + f"\n…[truncated {len(text) - _SHELL_MAX_OUTPUT} chars]"
        tag = f"exit {proc.returncode}" if proc.returncode else "ok"
        return f"$ {command}\n[{tag}] {text.strip() or '(no output)'}"
