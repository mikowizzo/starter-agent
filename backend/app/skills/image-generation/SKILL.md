---
name: image-generation
description: "Image generation and editing — create or modify images. Use when the user wants any visual artwork (paintings, watercolours, sketches, digital art, photography, illustrations) or wants to modify an existing image with a text prompt."
---

# Image Generation

Create and modify images via the Pollinations API. Two scripts handle everything — auth, API calls, saving to disk. You focus on the prompt and the display.

| Capability | Script | Endpoint | Model | When to use |
|------------|--------|----------|-------|-------------|
| **Generate** | `scripts/generate_image.py` | `/image/{prompt}` | zimage (default) | Creating new images from text descriptions |
| **Edit** | `scripts/edit_image.py` | `/v1/images/edits` | p-image-edit | Modifying an existing image with a text prompt |

## Shared: craft the prompt

Both capabilities start with a prompt. Transform the user's request into a vivid, descriptive instruction.

A prompt is ready when it names a **subject** + **medium** + at least two of **{style, lighting, composition, mood}**. For simple requests fewer modifiers are fine; for richer requests layer more.

**Prompt tips:**
- Lead with the subject, then layer medium and modifiers.
- Lighting adds depth — always include at least one lighting term.
- Name specific over generic: "amber streetlight" beats "yellow light."
- Specify paper or canvas texture for traditional media to add realism.
- For realism, add real camera settings — `f/1.8`, `85mm`, `ISO 100`, `35mm film` — the model reads them as "this is a photo" (full cheat sheet in `references/photorealism.md`).
- Negative prompts do NOT work with zimage — never write "no X" / "without Y"; zimage ignores them and is very sensitive to positive prompts, so anything described tends to appear. Write only what you WANT in the image (see `references/prompt-craft.md`).
- Naming an artist's style (e.g. "in the style of Studio Ghibli") is powerful but use sparingly.
- For known characters, let the name do the work — the model's internal reference is often more accurate than a long description. Lead with the name and only add details you want to *change* from the default.

For vocabulary — medium terms, lighting words, composition language, mood words, and aspect-ratio dimensions — consult `references/prompt-craft.md`.

For photorealistic versions of characters or avatar portraits (bridging an anime/game character into the real world), follow the ordered formula and worked example in `references/photorealism.md`.

**Completion:** the prompt is a single descriptive string with subject and medium explicit.

## Generate

### Call the script

```bash
python scripts/generate_image.py "<crafted prompt>" \
    --width 1024 --height 1024
```

Or use `--prompt` as a named argument (useful for programmatic calls):

```bash
python scripts/generate_image.py --prompt "<crafted prompt>"
```

No API key needed for the free URL path. If `POLLEN_API_KEY` is set, `generate_image.py` switches to the authenticated endpoint (`gen.pollinations.ai/v1/images/generations`) so the requested model is actually served — that's the only way to really get zimage. The authenticated path downloads the image bytes, saves them to the frontend public folder (`/workspace/frontend/public/generated/`), and prints a `/generated/<file>` display URL as the last line of stdout.

#### Model selection — LOCKED to zimage

**Only `zimage` (Z-Image Turbo, Alibaba) is allowed. The tool is sealed:** `--model` accepts exactly one choice, `zimage` — `argparse` rejects any other value (`flux`, `p-image`, anything else) with a usage error and exit code 2. There is no way to request another model via the CLI.

Single model per invocation — no automatic fallback. If generation fails, the script exits non-zero and we retry with zimage again after waiting.

**Known issue (Pollinations-side):** the free public endpoint (`image.pollinations.ai`) only lists `sana` and silently substitutes any other model name — confirmed: `model=zimage` returns `x-model-used: sana` in the response header — while the billing portal may still charge for zimage. That's a substitution/billing bug on Pollinations' side, not something we can fix in code. `generate_image.py` avoids it automatically when `POLLEN_API_KEY` is set (authenticated endpoint, real zimage). zimage is listed there as a paid image model (0.004 pollen/image token, supports `/v1/images/generations`, `/v1/images/edits`, `/image/{prompt}`).

**Completion:** the script exits `0` and the last stdout line is a display URL — a public `image.pollinations.ai` URL on the free path, or a `/generated/<file>` URL on the authenticated path (image saved to the frontend public folder). A data-URI appears only if the public folder is unwritable (a warning is printed to stderr). If exit ≠ `0`, read stderr and consult Failure handling — do not attempt to display anything that was not returned.

## Edit

Modify an existing image using a text prompt. The source image can be a local file or a URL; multiple source images are supported.

### Prepare the edit prompt

Write the edit instructions — describe *what to change* clearly. Good edit prompts are specific and instructional:

- "Replace the background with a misty mountain range at dawn"
- "Add a wide-brimmed straw hat to the figure"
- "Change the colour palette to warm autumn tones"

#### Prompting guideline for p-image-edit

For best results with p-image-edit, structure the prompt in three parts: **[Modification] → [Change Target] → [Preservation]**.

| Part | Purpose | Example |
|------|---------|---------|
| **[Modification]** | What to add, remove, or change | *Add a knitted purple teddy bear* |
| **[Change Target]** | Where or what to apply the change to | *next to the character reading a book* |
| **[Preservation]** | What must stay the same | *matching textures and fabric while preserving the overall style and keeping all other elements unchanged* |

Assembled: *"Add a knitted purple teddy bear next to the character reading a book, matching textures and fabric while preserving the overall style and keeping all other elements unchanged."*

The preservation clause is especially important — it anchors the model to the existing image and prevents unintended drift.

### Call the script

**Edit a local file** (uploaded as multipart/form-data):

```bash
python scripts/edit_image.py "<edit instructions>" \
    --image-file /workspace/frontend/public/generated/source.jpg
```

**Edit from an image URL** (sent as JSON):

```bash
python scripts/edit_image.py "<edit instructions>" \
    --image-url https://example.com/image.jpg
```

**Multiple source images** — repeat the flag:

```bash
python scripts/edit_image.py "<edit instructions>" \
    --image-file source_a.jpg --image-file source_b.jpg
```

The model `p-image-edit` is used automatically — there is no model selection or fallback for the edit endpoint. The script supports `--seed`, `--width`, `--height`, and `--output` just like generate.

**Completion:** the script exits `0` and the last stdout line is a display URL — the `/generated/<file>` URL of the edited image saved under `/workspace/frontend/public/generated/`. If exit ≠ `0`, read stderr and consult Failure handling.

## Display (both capabilities)

Render the image inline using the last line of stdout — do **not** base64-encode anything (that causes timeouts on full-resolution images):

```markdown
![artwork](<stdout-last-line>)
```

The last stdout line is always a URL you can drop straight into markdown:

- **Free generate path:** a public `image.pollinations.ai` URL.
- **Authenticated generate path / edit path:** a `/generated/<file>` URL — a same-origin relative path served by the frontend dev server from `/workspace/frontend/public/generated/`. It renders inline in the chat UI on any port (no hardcoded host, works in clones), needs no backend router, no auth, no proxy.

## Failure handling

- **403 / "error code: 1010"** — Cloudflare bot detection. The script already sends a browser User-Agent; if it recurs, treat as rate limiting and wait before retrying.
- **429 / 503** — rate limit or transient outage. The script retries once automatically; if it still fails, wait a bit and retry (zimage only — there is no fallback model).
- **401 / 402 / 403** — permanent errors. The script exits immediately without retrying. For 402, inform the user about insufficient balance.
- **Unexpected content-type** — the API returned an error JSON instead of an image. Stderr shows the response body.

If no file exists after the call, do not attempt to render anything — generation failed.

## Notes

- Both scripts print a display URL as their last stdout line for direct use in markdown: a public URL on the free path, a `/generated/<file>` URL on the authenticated/edit paths (saved to `/workspace/frontend/public/generated/`, served by the frontend at the site root).
- Generated images are ignored by git (see `.gitignore`).
- `--seed` produces reproducible results for the same prompt and model.
- Aspect ratios and their recommended dimensions are in `references/prompt-craft.md`.
