"""gi2_extract — deterministic HTML→visible-text companion extraction.

html-visible-v1 recipe (IMPROVEMENT-PLAN 4.1):
  - stdlib only (html.parser.HTMLParser) — NO readability ports, no
    heuristic drift: forensic determinism is the treaty (council r1).
  - skip script/style/noscript/template/svg (content-free for evidence)
  - newline on block-level boundary tags
  - collapse whitespace runs; strip per-line
  - JSON-LD appendix section (embedded structured data is quotable)
  - version-stamped: EXTRACTOR_VERSION = "html-visible-v1"
    + module code hash surfaced by the caller for provenance

The companion is a DERIVED CAS artifact: its sha256 is what claims
cite; verify only ever checks the cited hash (one-hash-one-claim).
"""

from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser

EXTRACTOR_VERSION = "html-visible-v1"

# Tags whose entire subtree is content-free for quoting purposes.
_SKIP_SUBTREE = frozenset({"script", "style", "noscript", "template", "svg", "head"})

# Block-level boundary tags: emit a newline when they open AND close,
# so sibling blocks never fuse into one line.
_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "body", "br", "button",
    "caption", "center", "dd", "details", "dialog", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
    "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "html", "input",
    "label", "legend", "li", "main", "menu", "nav", "ol", "option", "p",
    "pre", "section", "select", "summary", "table", "tbody", "td",
    "textarea", "tfoot", "th", "thead", "tr", "ul",
})

# NBSP (\xa0) collapses too: convert_charrefs decodes &nbsp; into it,
# and visible text should not carry hard non-breaking spaces (f4 lesson).
_WHITESPACE_RUN = re.compile(r"[ \t\r\f\v\xa0]+")


class _VisibleTextParser(HTMLParser):
    """Collects visible text with block-boundary newlines + JSON-LD."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip_depth = 0
        self._jsonld_depth = 0
        self._jsonld_chunks: list[str] = []

    # -- tag events ------------------------------------------------------

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "script":
            # script subtree: JSON-LD captured separately, others skipped
            at = dict(attrs)
            t = (at.get("type") or "").strip().lower()
            if t in ("", "text/html", "application/ld+json"):
                if t == "application/ld+json":
                    self._jsonld_depth = self._skip_depth
                    self._jsonld_chunks.append("")
                    self._enter_skip()
                    return
            self._enter_skip()
            return
        if tag in _SKIP_SUBTREE or (self._skip_depth and tag == "script"):
            self._enter_skip()
            return
        if self._skip_depth:
            return
        if tag in _BLOCK_TAGS:
            self._out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            if self._jsonld_chunks and self._jsonld_depth == self._skip_depth - 1:
                self._jsonld_depth = 0
            self._leave_skip()
            return
        if self._skip_depth:
            if tag in _SKIP_SUBTREE:
                self._leave_skip()
            return
        if tag in _BLOCK_TAGS:
            self._out.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        # self-closing (br/, hr/, img/): boundary if block
        if not self._skip_depth and tag in _BLOCK_TAGS:
            self._out.append("\n")

    # -- character data --------------------------------------------------

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            if self._jsonld_chunks and self._jsonld_depth == self._skip_depth - 1:
                self._jsonld_chunks[-1] += data
            return
        if data:
            self._out.append(data)

    # -- skip bookkeeping --------------------------------------------------

    def _enter_skip(self) -> None:
        self._skip_depth += 1

    def _leave_skip(self) -> None:
        if self._skip_depth:
            self._skip_depth -= 1


def extract_visible_text(html: str) -> str:
    """Return deterministic visible text for HTML input.

    Rules: subtree skips, block newlines, whitespace-run collapse,
    per-line strip, blank-line collapse, JSON-LD appendix.
    """
    parser = _VisibleTextParser()
    parser.feed(html)
    parser.close()

    # finish any JSON-LD capture
    lines: list[str] = []
    text = "".join(parser._out)
    for line in text.split("\n"):
        line = _WHITESPACE_RUN.sub(" ", line).strip()
        if line:
            lines.append(line)

    jsonld = [c.strip() for c in parser._jsonld_chunks if c.strip()]
    if jsonld:
        lines.append("## JSON-LD")
        for blob in jsonld:
            lines.extend(blob.splitlines())

    return "\n".join(lines) + ("\n" if lines else "")


def extractor_code_hash() -> str:
    """Stable hash of this module's own source — provenance stamp."""
    with open(__file__, "rb") as fh:
        return "sha256:" + hashlib.sha256(fh.read()).hexdigest()


def is_html(blob: bytes, content_type: str | None = None) -> bool:
    """Heuristic HTML detection for companion routing (4.4)."""
    if content_type and "html" in content_type.lower():
        return True
    head = blob[:4096].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<body")) or (
        b"<html" in head or (b"<body" in head)
    )
