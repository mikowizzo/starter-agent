"""Golden tests for poneglyph_extract.html-visible-v1 (IMPROVEMENT-PLAN 4.1).

Run: python3 test_poneglyph_extract.py   (exit 0 = pass)
Deterministic by construction: no randomness, no network, stdlib only.
"""

import hashlib
import sys

import poneglyph_extract as gx

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# ---- fixture 1: basic block separation + inline text -------------------
H1 = """<html><head><title>Ignored Title</title><style>body{color:red}</style></head>
<body><h1>Hello</h1><p>World of <em>evidence</em>.</p><div>Plain div</div></body></html>"""
E1 = gx.extract_visible_text(H1)
check("f1 block separation", E1.splitlines() == ["Hello", "World of evidence.", "Plain div"], repr(E1))
check("f1 title/style skipped", "Ignored" not in E1 and "color:red" not in E1)

# ---- fixture 2: script subtree fully skipped ----------------------------
H2 = """<body><p>Before</p><script>var x = 1; if (a < b) { evil("</p>not visible</p>"); }</script><p>After</p></body>"""
E2 = gx.extract_visible_text(H2)
check("f2 script skipped", "var x" not in E2 and "not visible" not in E2)
check("f2 neighbours intact", E2.splitlines() == ["Before", "After"], repr(E2))

# ---- fixture 3: JSON-LD appendix ---------------------------------------
H3 = """<body><p>Story text.</p><script type="application/ld+json">{"headline": "AI Weekly"}</script></body>"""
E3 = gx.extract_visible_text(H3)
check("f3 jsonld appended", "## JSON-LD" in E3 and '"AI Weekly"' in E3, repr(E3))
check("f3 body first", E3.splitlines()[0] == "Story text.")

# ---- fixture 4: whitespace collapse + entity refs ----------------------
H4 = "<p>Cost&nbsp;&amp;&nbsp;price:  $0.14&ndash;$0.22  per M</p>"
E4 = gx.extract_visible_text(H4)
check("f4 entities decoded + ws collapse", E4.strip() == "Cost & price: $0.14–$0.22 per M", repr(E4))

# ---- fixture 5: determinism (double-run byte identity) ------------------
H5 = open(__file__.replace("test_poneglyph_extract.py", "poneglyph_extract.py"), "rb").read().decode()
D1 = hashlib.sha256(gx.extract_visible_text(H5).encode()).hexdigest()
D2 = hashlib.sha256(gx.extract_visible_text(H5).encode()).hexdigest()
check("f5 deterministic", D1 == D2)

# ---- fixture 6: real artifact regression — case ai HTML companion ------
import pathlib
store = pathlib.Path(__file__).resolve().parent.parent / "cases" / "ai" / "evidence" / "sha256"
html_blobs: list[pathlib.Path] = []
if store.exists():
    for shard in sorted(store.rglob("*")):
        if shard.is_file():
            try:
                blob = shard.read_bytes()
            except OSError:
                continue
            if blob[:2048].lstrip().lower().startswith((b"<!doctype html", b"<html")) or b"<html" in blob[:4096]:
                html_blobs.append(shard)
check("f6 case artifacts found", len(html_blobs) > 0, f"{len(html_blobs)} html blobs")
ratio_worst = 1.0
for p in html_blobs[:10]:
    raw = p.read_bytes()
    text = gx.extract_visible_text(raw.decode("utf-8", errors="replace"))
    ratio = len(text.encode()) / max(1, len(raw))
    ratio_worst = min(ratio_worst, ratio)
    check(
        f"f6 shrink {p.name[:12]}",
        0 < ratio < 1.0,
        f"ratio={ratio:.3f}",
    )
print(f"      (worst shrink ratio: {ratio_worst:.3f} — expect <<1.0)")

# ---- fixture 7: is_html heuristics --------------------------------------
check("f7 html by sniff", gx.is_html(b"  <!doctype html><html>", None))
check("f7 html by ctype", gx.is_html(b"\x00binary", "text/html; charset=utf-8"))
check("f7 json not html", not gx.is_html(b'{"a":1}', "application/json"))
check("f7 pdf not html", not gx.is_html(b"%PDF-1.7", "application/pdf"))

# ---- version stamp -------------------------------------------------------
check("f8 version", gx.EXTRACTOR_VERSION == "html-visible-v1")
check("f8 code hash stable", gx.extractor_code_hash().startswith("sha256:"))

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
print("ALL TESTS PASSED")
