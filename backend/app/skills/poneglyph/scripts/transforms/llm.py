#!/usr/bin/env python3
"""
llm.py — GI v2 LLM Reader Transform (Architecture B: egress exception).

Design (see references/LLM-READER.md):
- The transform itself calls an OpenAI-compatible chat-completions endpoint.
  The jail is contractual, not enforced; the gate re-verifies every quote
  against the host-stored artifact, so a lying or hallucinating model can
  waste its own time but cannot poison the journal.
- Determinism of the RECORD: a second run may produce different claims —
  that is model non-determinism, and it is fine. Each run's accepted claims
  are frozen verbatim in the append-only journal with via_run provenance;
  replay never re-executes the transform.

Model routing (first match wins):
  1. OPENAI_BASE_URL / LLM_API_BASE env  → explicit override (tests use this)
  2. model id kimi*                      → https://api.synthetic.new/v1 (SYNTHETIC_API_KEY)
  3. model id deepseek*                  → https://opencode.ai/zen/go/v1 (OPENCODE_API_KEY)
                                           (no hf: prefix needed — ids are bare on OpenCode)
  4. anything else                       → https://opencode.ai/zen/go/v1 (OPENCODE_API_KEY)
                                           fallback https://openrouter.ai/api/v1 (OPENROUTER_API_KEY)

Args (--arg K=V): model, focus, max_chars (24000), max_claims (30),
                  repair (true|false, default true).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any

DEFAULT_MAX_CHARS = 24000
DEFAULT_MAX_CLAIMS = 30
CONFIDENCE_CAP = 0.8
DEFAULT_MODEL = "deepseek-v4-flash"
CALL_TIMEOUT_S = 60
DEFAULT_CALL_TIMEOUT_S = 180


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def route_model(model: str) -> tuple[str, str, str]:
    """Resolve (base_url, api_key, real_model_id). Explicit env wins."""
    override = os.environ.get("OPENAI_BASE_URL") or os.environ.get("LLM_API_BASE")
    if override:
        key = (os.environ.get("OPENAI_API_KEY")
               or os.environ.get("SYNTHETIC_API_KEY")
               or os.environ.get("OPENCODE_API_KEY")
               or os.environ.get("OPENROUTER_API_KEY")
               or "")
        return override, key, model
    if model.startswith("kimi"):
        # Synthetic wants the hf: form; accept bare aliases like kimi-k3.
        real = "hf:moonshotai/Kimi-K3" if "/" not in model else model
        return "https://api.synthetic.new/v1", os.environ.get("SYNTHETIC_API_KEY", ""), real
    if os.environ.get("OPENCODE_API_KEY"):
        return "https://opencode.ai/zen/go/v1", os.environ["OPENCODE_API_KEY"], model
    return "https://openrouter.ai/api/v1", os.environ.get("OPENROUTER_API_KEY", ""), model


def find_verbatim_span(artifact_text: str, quote: str,
                       hint_start: int | None = None,
                       hint_end: int | None = None) -> tuple[int, int] | None:
    """Locate a quote in text: exact-at-hint → exact-search → whitespace-tolerant."""
    if not quote or not quote.strip():
        return None
    if (hint_start is not None and hint_end is not None
            and 0 <= hint_start < hint_end <= len(artifact_text)
            and artifact_text[hint_start:hint_end] == quote):
        return hint_start, hint_end
    idx = artifact_text.find(quote)
    if idx != -1:
        return idx, idx + len(quote)
    norm_quote = re.sub(r"\s+", " ", quote).strip()
    if not norm_quote:
        return None
    pattern = re.sub(r"\\ ", r"\\s+", re.escape(norm_quote))
    m = re.search(pattern, artifact_text)
    if m:
        return m.start(), m.end()
    return None


def call_chat_completion(api_base: str, api_key: str, model: str,
                         messages: list[dict[str, str]],
                         temperature: float = 0.0) -> tuple[str, dict[str, Any]]:
    url = api_base.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    payload = {"model": model, "messages": messages, "temperature": temperature}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "gi2-llm-transform/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=CALL_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} from model endpoint: {err_body[:500]}") from e
    except Exception as e:
        raise RuntimeError(f"cannot reach model endpoint: {e}") from e
    choices = data.get("choices", [])
    if not choices:
        raise RuntimeError("no choices returned from model endpoint")
    return choices[0].get("message", {}).get("content", ""), data.get("usage", {})


def parse_model_ndjson(text: str) -> list[dict[str, Any]]:
    """Parse model output into op dicts; tolerates markdown fencing."""
    cleaned = re.sub(r"^```(?:json|ndjson)?", "", text.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"^```$", "", cleaned, flags=re.MULTILINE)
    results: list[dict[str, Any]] = []
    for line in cleaned.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            if isinstance(item, dict) and "op" in item:
                results.append(item)
        except json.JSONDecodeError:
            continue
    return results


def build_system_prompt(focus: str | None, truncated: bool) -> str:
    focus_clause = f"\nFocus your extraction on: {focus}." if focus else ""
    trunc_note = ("\nYou are reading a PREFIX of a longer document; only cite "
                  "from what you see.") if truncated else ""
    return (
        "You are a strict forensic extraction assistant. Read the provided text and "
        f"emit ONLY NDJSON records for entities and claims supported by the text.{focus_clause}{trunc_note}\n\n"
        "Rules:\n"
        "1. Every claim MUST include a verbatim 'quote' copied exactly from the text.\n"
        "2. Include 'span_start' and 'span_end' character offsets into the EXACT provided text.\n"
        "3. Never paraphrase or fabricate quotes. If the text does not support it, omit it.\n"
        "4. Entity ids must be '<kind>:<slug>'; kinds: person|org|location|thing.\n"
        "5. Output format, one JSON object per line, no prose, no markdown:\n"
        '   {"op":"entity","id":"person:jane","name":"Jane Doe","kind":"person","attrs":{}}\n'
        '   {"op":"claim","subj":"person:jane","pred":"works_at","obj":"org:acme",'
        '"polarity":"supports","confidence":0.7,"quote":"Jane works at Acme","span_start":123,"span_end":139}\n'
    )


def validate_and_filter_items(
    items: list[dict[str, Any]], text: str, max_claims: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split items into (entities, accepted_claims, rejected_claims).

    Enforces the LLM policy: evidence=inferred always, confidence capped at
    0.8, quote must be locatable verbatim (exact or whitespace-tolerant).
    Spans are re-derived from the actual text so the gate always passes.
    """
    entities: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen_eids: set[str] = set()
    for item in items:
        op = item.get("op")
        if op == "entity":
            eid, name = item.get("id"), item.get("name")
            if eid and name and eid not in seen_eids:
                seen_eids.add(eid)
                entities.append(item)
        elif op == "claim":
            if len(accepted) >= max_claims:
                item["_rejection_reason"] = "max_claims cap reached"
                rejected.append(item)
                continue
            subj, pred, obj = item.get("subj"), item.get("pred"), item.get("obj")
            quote = item.get("quote", "")
            if not subj or not pred or not obj or not quote:
                item["_rejection_reason"] = "missing subj/pred/obj/quote"
                rejected.append(item)
                continue
            raw_conf = item.get("confidence", CONFIDENCE_CAP)
            try:
                conf = min(float(raw_conf), CONFIDENCE_CAP)
            except (TypeError, ValueError):
                conf = CONFIDENCE_CAP
            conf = max(0.0, conf)
            span = find_verbatim_span(text, quote, item.get("span_start"), item.get("span_end"))
            if span is None:
                item["_rejection_reason"] = f"quote not found verbatim: {str(quote)[:60]!r}"
                rejected.append(item)
                continue
            ss, se = span
            accepted.append({
                "op": "claim",
                "subj": subj, "pred": pred, "obj": obj,
                "polarity": item.get("polarity", "supports"),
                "evidence": "inferred",
                "confidence": conf,
                "quote": text[ss:se],
                "span_start": ss, "span_end": se,
            })
    return entities, accepted, rejected


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
    except Exception as e:
        print(f"error parsing stdin: {e}", file=sys.stderr)
        sys.exit(1)

    artifact_text = payload.get("artifact_text", "")
    args = payload.get("args", {}) or {}

    max_chars = int(args.get("max_chars", DEFAULT_MAX_CHARS))
    max_claims = int(args.get("max_claims", DEFAULT_MAX_CLAIMS))
    model = args.get("model", DEFAULT_MODEL)
    focus = args.get("focus")
    enable_repair = str(args.get("repair", "true")).lower() in ("true", "1", "yes")
    global CALL_TIMEOUT_S
    CALL_TIMEOUT_S = int(args.get("call_timeout", DEFAULT_CALL_TIMEOUT_S))

    truncated_text = artifact_text[:max_chars]
    truncated = len(artifact_text) > max_chars

    api_base, api_key, model = route_model(str(model))
    if not api_key:
        print(json.dumps({"op": "log", "level": "error",
                          "message": f"no API key for model {model} (route {api_base})"}))
        sys.exit(1)

    system_prompt = build_system_prompt(focus, truncated)
    prompt_hash = sha256_text(system_prompt)[:16]
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Document text:\n{truncated_text}"},
    ]

    # Round 1 — extraction
    try:
        content_r1, usage_r1 = call_chat_completion(api_base, api_key, str(model), messages)
    except Exception as e:
        print(json.dumps({"op": "log", "level": "error", "message": str(e)}))
        sys.exit(1)
    items_r1 = parse_model_ndjson(content_r1)
    entities, accepted, rejected = validate_and_filter_items(items_r1, truncated_text, max_claims)

    prompt_tokens = usage_r1.get("prompt_tokens", 0)
    completion_tokens = usage_r1.get("completion_tokens", 0)

    # Round 2 — single optional repair for rejected claims
    if enable_repair and rejected and len(accepted) < max_claims:
        repair_request = [{
            "subj": r.get("subj"), "pred": r.get("pred"), "obj": r.get("obj"),
            "failed_quote": r.get("quote"),
            "reason": r.get("_rejection_reason"),
        } for r in rejected]
        repair_messages = messages + [
            {"role": "assistant", "content": content_r1},
            {"role": "user", "content": (
                "These claims were REJECTED because their quotes could not be verified "
                f"verbatim:\n{json.dumps(repair_request, indent=2)}\n\n"
                "Fix them by quoting the EXACT text (with correct spans), or omit any "
                "claim the text does not support. Return ONLY NDJSON.")},
        ]
        try:
            content_r2, usage_r2 = call_chat_completion(api_base, api_key, str(model), repair_messages)
            prompt_tokens += usage_r2.get("prompt_tokens", 0)
            completion_tokens += usage_r2.get("completion_tokens", 0)
            new_ents, repaired, _still = validate_and_filter_items(
                parse_model_ndjson(content_r2), truncated_text,
                max_claims - len(accepted))
            have = {e["id"] for e in entities}
            entities.extend(e for e in new_ents if e["id"] not in have)
            accepted.extend(repaired)
        except Exception as e:
            print(json.dumps({"op": "log", "level": "warn",
                              "message": f"repair round failed: {e}"}))

    for ent in entities:
        print(json.dumps({
            "op": "entity", "id": ent["id"], "name": ent["name"],
            "kind": ent.get("kind", "entity"), "attrs": ent.get("attrs", {}),
        }))
    for clm in accepted:
        print(json.dumps(clm))
    print(json.dumps({
        "op": "log", "level": "info",
        "message": (f"llm reader done: model={model} prompt_hash={prompt_hash} "
                    f"tokens={prompt_tokens + completion_tokens} "
                    f"(in={prompt_tokens} out={completion_tokens}) "
                    f"entities={len(entities)} accepted={len(accepted)} "
                    f"rejected_final={len(rejected)}"),
    }))


if __name__ == "__main__":
    main()
