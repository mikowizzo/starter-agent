#!/usr/bin/env python3
"""
dig.py — GI v2 Slice 8: the prospect transform (untrusted novelty feed).

Design (see references/DIG.md):
- Input is a HOST-BUILT snapshot artifact: direct/inferred CITED claims +
  entities (hypothesis claims and prior dig events are EXCLUDED — the model
  must not read its own offspring as landscape).
- The transform calls an OpenAI-compatible endpoint (same egress-exception
  pattern as llm.py: contractual jail, host-side mechanical gate).
- Output is NDJSON prospect records. The HOST re-validates every field
  mechanically: anchors must resolve to ACTIVE cited claims, entities must
  exist, dedupe against existing edges, quota, H:D ratio cap. Nothing the
  model says about itself (novelty, grounding) is trusted.
- Prospect status is derived at query time from journal events; the pack is
  append-only. dig accept/test/withdraw are the analyst's speech acts.

Args (--arg K=V): model (kimi-k3), focus, max_prospects (6), call_timeout (300).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.request
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm import (  # noqa: E402
    call_chat_completion,
    parse_model_ndjson,
    route_model,
)

DEFAULT_MAX_PROSPECTS = 6
DEFAULT_MODEL = "kimi-k3"
DIG_TIMEOUT_S = 300


def log(level: str, msg: str) -> None:
    print(json.dumps({"op": "log", "level": level, "message": msg}))


def build_snapshot_digest(artifact_text: str, args: dict) -> str:
    """Render the snapshot artifact into a compact, quotable digest for the
    model: entity list + one line per cited claim with its claim_id."""
    try:
        snap = json.loads(artifact_text)
    except json.JSONDecodeError as e:
        log("error", f"snapshot artifact is not valid JSON: {e}")
        sys.exit(1)
    lines = ["ENTITIES:"]
    for e in snap.get("entities", []):
        lines.append(f"  {e['id']}  ({e.get('kind', 'entity')}) {e.get('name', '')}")
    lines.append("CLAIMS (each has a claim_id you may anchor to):")
    for c in snap.get("claims", []):
        conf = c.get("confidence")
        conf_s = f" conf={conf}" if conf is not None else ""
        q = (c.get("quote") or "").replace("\n", " ")
        if len(q) > 90:
            q = q[:87] + "..."
        lines.append(
            f"  [{c['claim_id']}] {c['subj']} {c['pred']} {c['obj']} "
            f"({c.get('evidence', 'inferred')}{conf_s})\n    quote: {q}"
        )
    lines.append(f"SNAPSHOT_HASH: {snap.get('snapshot_hash', '?')}")
    focus = args.get("focus")
    if focus:
        lines.append(f"FOCUS: {focus}")
    return "\n".join(lines)


SYSTEM_PROMPT = """You are the prospecting engine of a forensic knowledge journal. \
Read the investigation snapshot (entities + cited claims, each with a claim_id) and \
generate NOVEL research prospects: candidate edges the evidence points toward but \
does not yet establish.

Discipline (generate, then self-filter before emitting):
- Diverge using these lenses, in order of trustworthiness: (1) signal tracing — \
follow a surprising observation to its mechanism; (2) constraint relaxation — \
drop a routine assumption and see what becomes testable; (3) scale jumping — \
move a mechanism up/down one level (cellular to behavioral, dyadic to social); \
(4) failure-mode inversion — ask what evidence would FALSIFY a current belief; \
(5) conceptual blending — combine two distant mechanisms, ONLY when both are \
anchored (last resort: analogies that feel mechanistic but are decorative are \
the top source of confident-wrong leads).
- Every prospect must ANCHOR its load-bearing premises to claim_ids in the \
snapshot. If you cannot anchor it, it is speculation: do not emit it.
- Every prospect must state a KILL CRITERION: a concrete observable that would \
retract it, and the source class that could settle it.
- No restating an existing claim/edge with loftier language. Novelty is against \
the snapshot's existing (subj, pred) pairs.
- Effect size must matter for decisions the case's principal actually makes.

Emit ONLY NDJSON records, one JSON object per line, no prose, no markdown:
{"op":"prospect","subj":"thing:existing-or-new-entity","pred":"may_enable","obj":"thing:...","thesis":"one sentence","mechanism":"2-3 sentences, citing claim_ids inline as [c_...]","anchors":["c_..."],"kill_criterion":{"observation":"what observation would retract this","polarity":"refutes","source_class":"e.g. systematic review"},"fetch_targets":["source classes, no URLs"],"novelty_against":["c_..."]}

Rules for the triple: subj/obj must be existing entity ids from the snapshot, \
or 'thing:<slug>' for a NEW entity the prospect introduces. pred should read \
as 'test me': may_enable, may_drive, may_prevent, may_predict, may_cost, \
may_explain."""


def main() -> None:
    payload = json.loads(sys.stdin.read())
    artifact_text = payload.get("artifact_text", "")
    args = payload.get("args") or {}

    model = args.get("model", DEFAULT_MODEL)
    max_prospects = int(args.get("max_prospects", DEFAULT_MAX_PROSPECTS))
    timeout_s = int(args.get("call_timeout", DIG_TIMEOUT_S))
    focus = args.get("focus")

    digest = build_snapshot_digest(artifact_text, args)

    api_base, api_key, real_model = route_model(model)
    if not api_key:
        log("error", f"no API key for route {api_base}")
        sys.exit(1)

    seed = int(args.get("seed", "0"))
    user_msg = (
        f"Snapshot digest:\n\n{digest}\n\n"
        f"Emit up to {max_prospects} prospect records as NDJSON."
    )
    prompt_sha256 = hashlib.sha256(
        (SYSTEM_PROMPT + "\x00" + user_msg).encode("utf-8")).hexdigest()
    # run_meta is REPORTED, not trusted: the host journals it for audit, and
    # code+args+snapshot are enough to reproduce and cross-check it.
    print(json.dumps({"op": "run_meta", "model": real_model,
                      "prompt_sha256": prompt_sha256, "seed": seed,
                      "temperature": 0.4}))
    content, _usage = call_chat_completion(
        api_base, api_key, real_model,
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": user_msg}],
        temperature=0.4,
    )
    items = parse_model_ndjson(content)
    emitted = 0
    for item in items:
        if item.get("op") == "prospect":
            print(json.dumps(item, ensure_ascii=False))
            emitted += 1
        elif item.get("op") == "log":
            print(json.dumps(item))
    if emitted == 0:
        log("warning", "model returned no prospect records")
        sys.exit(1)


if __name__ == "__main__":
    main()
