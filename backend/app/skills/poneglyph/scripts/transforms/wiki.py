#!/usr/bin/env python3
"""
wiki.py — GI v2 Wikipedia HTML Transform (stdlib html.parser only).

Parses Wikipedia HTML: extracts page title entity and Infobox table rows,
matching verbatim text offsets against artifact_text.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from html.parser import HTMLParser

LABEL_MAP = {
    "founded": "founded",
    "headquarters": "headquarters",
    "founder": "founder",
    "founders": "founders",
    "founder(s)": "founders",
    "revenue": "revenue",
    "key people": "key_people",
    "industry": "industry",
    "website": "website",
    "num employees": "num_employees",
    "number of employees": "num_employees",
    "employees": "num_employees",
}


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "unnamed"


class WikiInfoboxParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.in_title = False
        self.in_infobox = 0
        self.in_th = False
        self.in_td = False
        self.current_th: list[str] = []
        self.current_td: list[str] = []
        self.infobox_rows: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "title":
            self.in_title = True
        if tag == "table":
            classes = attrs_dict.get("class", "").split()
            if "infobox" in classes:
                self.in_infobox += 1
            elif self.in_infobox > 0:
                self.in_infobox += 1
        elif self.in_infobox > 0:
            if tag == "th":
                self.in_th = True
                self.current_th = []
            elif tag == "td":
                self.in_td = True
                self.current_td = []

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag == "table" and self.in_infobox > 0:
            self.in_infobox -= 1
        elif self.in_infobox > 0:
            if tag == "th":
                self.in_th = False
            elif tag == "td":
                self.in_td = False
            elif tag == "tr":
                th_text = re.sub(r"\s+", " ", "".join(self.current_th)).strip()
                td_text = re.sub(r"\s+", " ", "".join(self.current_td)).strip()
                if th_text and td_text:
                    self.infobox_rows.append((th_text, td_text))
                self.current_th = []
                self.current_td = []

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.in_th:
            self.current_th.append(data)
        if self.in_td:
            self.current_td.append(data)


def main():
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            return
        payload = json.loads(raw_input)
    except Exception as e:
        print(f"error parsing stdin: {e}", file=sys.stderr)
        sys.exit(1)

    artifact_text = payload.get("artifact_text", "")
    parser = WikiInfoboxParser()
    parser.feed(artifact_text)

    clean_title = re.sub(r"\s+-\s+Wikipedia.*$", "", parser.title, flags=re.IGNORECASE).strip()
    if not clean_title:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", artifact_text, flags=re.DOTALL | re.IGNORECASE)
        if m:
            clean_title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        else:
            clean_title = "Unknown Page"

    page_slug = slugify(clean_title)
    page_id = f"thing:{page_slug}"

    print(json.dumps({
        "op": "entity",
        "id": page_id,
        "name": clean_title,
        "kind": "thing",
        "attrs": {"source": payload.get("uri")},
    }))

    claims_emitted = 0
    for label, val in parser.infobox_rows:
        canon_label = label.lower().strip()
        matched_pred = None
        for k, v in LABEL_MAP.items():
            if k in canon_label:
                matched_pred = f"attr:{v}"
                break
        if not matched_pred:
            matched_pred = f"attr:{slugify(label)}"

        # Find value in verbatim text for quote span
        idx = artifact_text.find(val)
        if idx == -1:
            # Try sub-phrase if whitespace normalized
            val_norm = re.sub(r"\s+", " ", val).strip()
            # fallback search
            idx = artifact_text.find(val_norm)
            if idx != -1:
                val = val_norm

        if idx == -1:
            continue

        span_start = idx
        span_end = idx + len(val)

        # Literal entity
        val_hash = hashlib.sha1(val.encode("utf-8")).hexdigest()[:10]
        lit_id = f"literal:{val_hash}"

        print(json.dumps({
            "op": "entity",
            "id": lit_id,
            "name": val,
            "kind": "literal",
            "attrs": {},
        }))

        print(json.dumps({
            "op": "claim",
            "subj": page_id,
            "pred": matched_pred,
            "obj": lit_id,
            "polarity": "supports",
            "evidence": "direct",
            "confidence": 1.0,
            "quote": val,
            "span_start": span_start,
            "span_end": span_end,
        }))
        claims_emitted += 1

    print(json.dumps({
        "op": "log",
        "level": "info",
        "message": f"extracted page {page_id} with {claims_emitted} claims from infobox",
    }))


if __name__ == "__main__":
    main()
