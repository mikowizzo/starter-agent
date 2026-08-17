#!/usr/bin/env python3
"""
_liar.py — Test fixture transform emitting 3 fabricated claims and 1 valid claim.
"""

from __future__ import annotations

import json
import sys


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    artifact_text = payload.get("artifact_text", "")

    # Register entities needed for the claims
    print(json.dumps({"op": "entity", "id": "thing:target", "name": "Target", "kind": "thing"}))
    print(json.dumps({"op": "entity", "id": "literal:valid", "name": "Valid Value", "kind": "literal"}))
    print(json.dumps({"op": "entity", "id": "literal:bogus1", "name": "Bogus 1", "kind": "literal"}))
    print(json.dumps({"op": "entity", "id": "literal:bogus2", "name": "Bogus 2", "kind": "literal"}))
    print(json.dumps({"op": "entity", "id": "literal:bogus3", "name": "Bogus 3", "kind": "literal"}))

    # 1. Valid claim citing real substring
    if len(artifact_text) >= 10:
        quote = artifact_text[:10]
        print(json.dumps({
            "op": "claim",
            "subj": "thing:target",
            "pred": "attr:valid",
            "obj": "literal:valid",
            "polarity": "supports",
            "evidence": "direct",
            "confidence": 1.0,
            "quote": quote,
            "span_start": 0,
            "span_end": 10,
        }))

    # 2. Liar claim 1: fabricated quote text
    print(json.dumps({
        "op": "claim",
        "subj": "thing:target",
        "pred": "attr:bogus1",
        "obj": "literal:bogus1",
        "polarity": "supports",
        "evidence": "direct",
        "confidence": 1.0,
        "quote": "THIS_TEXT_DEFINITELY_DOES_NOT_EXIST_IN_ARTIFACT_12345",
        "span_start": 0,
        "span_end": 10,
    }))

    # 3. Liar claim 2: out of bounds span
    print(json.dumps({
        "op": "claim",
        "subj": "thing:target",
        "pred": "attr:bogus2",
        "obj": "literal:bogus2",
        "polarity": "supports",
        "evidence": "direct",
        "confidence": 1.0,
        "quote": "some quote",
        "span_start": 999999,
        "span_end": 1000020,
    }))

    # 4. Liar claim 3: negative / inverted span
    print(json.dumps({
        "op": "claim",
        "subj": "thing:target",
        "pred": "attr:bogus3",
        "obj": "literal:bogus3",
        "polarity": "supports",
        "evidence": "direct",
        "confidence": 1.0,
        "quote": "some quote",
        "span_start": 20,
        "span_end": 10,
    }))


if __name__ == "__main__":
    main()
