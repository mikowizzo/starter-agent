#!/usr/bin/env python3
"""transform-wiki — GI transform: Wikipedia REST summary -> NDJSON.

Contract (transforms are standalone executables; they NEVER touch the case
file): read one entity JSON on stdin, write NDJSON to stdout, drop fetched
artifacts where the runner tells you to.

stdin:  {"entity": {"id": ..., "name": ...}, "evidence_dir": "...", "config": {...}}
stdout: {"type":"artifact", "uri":..., "fetched_at":..., "file":<tmp>, "hash":"sha256:.."}
        {"type":"entity", "id":..., "name":..., "kind":..., ...}
        {"type":"claim", "subj":..., "pred":..., "obj":..., "evidence":"inferred",
         "confidence":0.6, "cites":[{"artifact":"sha256:..","span":[s,e],"quote":"..."}]}

The hash is recomputed at ingest; the quote is verified against the span.
Evidence type is "inferred" — wiki prose is a first-pass map, never a
conclusion, and the regex extraction derives structure from prose.
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://en.wikipedia.org/api/rest_v1/page/summary/"
UA = "gi-transform-wiki/0.1 (+https://example.com; educational research)"


def normalize_ws(s: str) -> str:
    """Must match gi.py's normalize_ws exactly — spans are in this space."""
    return re.sub(r"\s+", " ", s).strip()


def slugify(name: str) -> str:
    s = name.casefold()
    s = "".join(c if c.isalnum() else "-" for c in s)
    return re.sub(r"-+", "-", s).strip("-")


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")


def fetch(title: str) -> dict:
    url = API + urllib.parse.quote(title.replace(" ", "_"))
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


# (pred, regex with one capture group, evidence) — one claim per pattern max.
# Capture groups start with [^\W\d_] (any unicode letter): "Ōta" must match.
PATTERNS = [
    ("headquartered_in",
     r"headquartered (?:in|at) ([^\W\d_][\w\s,'\-]{2,60}?)(?:[,.]|\s+and\s)", "inferred"),
    ("subsidiary_of",
     r"(?:is |a )?subsidiary of ([^\W\d_][\w\s.'\-]{2,60}?)(?:[,.]|\s+and\s)", "inferred"),
    ("founded_by",
     r"founded by ([^\W\d_][\w\s.'\-]{2,60}?)(?:[,.]|\s+in\s|\s+in\s\d)", "inferred"),
]


def sentence_around(text: str, pos: int) -> tuple:
    """(start, end) of the sentence containing pos, in normalized text."""
    starts = [m.end() for m in re.finditer(r"[.!?]\s+", text[:pos])]
    ends = [m.start() for m in re.finditer(r"[.!?]\s+", text[pos:])]
    s = starts[-1] if starts else 0
    e = (pos + ends[0] + 1) if ends else len(text)
    return s, e


def main() -> int:
    payload = json.load(sys.stdin)
    ent = payload["entity"]
    evidence_dir = Path(payload["evidence_dir"])
    title = ent.get("name") or ent.get("id")
    try:
        page = fetch(title)
    except Exception as e:
        print(f"WARNING: wiki fetch failed for {title}: {e}", file=sys.stderr)
        return 0
    if page.get("type") == "disambiguation":
        print(f"WARNING: {title} is a disambiguation page; skipped", file=sys.stderr)
        return 0
    extract = page.get("extract") or ""
    if not extract:
        print(f"WARNING: no extract for {title}", file=sys.stderr)
        return 0

    canonical = page.get("title") or title
    raw = extract.encode("utf-8")
    tmp = evidence_dir / f"tmp-{os.getpid()}-{int(time.time())}.txt"
    tmp.write_bytes(raw)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    emit({"type": "artifact",
          "uri": API + urllib.parse.quote(canonical.replace(" ", "_")),
          "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
          "file": str(tmp), "hash": digest})

    subj = slugify(canonical)
    emit({"type": "entity", "id": subj, "name": canonical,
          "kind": "organization", "attrs": {"wiki_title": canonical},
          "external_ids": {}, "aliases": []})

    text = normalize_ws(extract)
    for pred, pat, evidence in PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if not m:
            continue
        obj_name = m.group(1).strip(" .")
        s, e = sentence_around(text, m.start())
        quote = text[s:e].strip()
        emit({"type": "claim", "subj": subj, "pred": pred,
              "obj": slugify(obj_name), "polarity": "supports",
              "evidence": evidence, "confidence": 0.6,
              "cites": [{"artifact": digest, "span": [s, e], "quote": quote}]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
