# Photorealism — photorealistic character avatars

Recipe for turning a fictional character into a stunning photorealistic portrait, refined while crafting Nami's avatar (HyunA-inspired). Use when the user asks for a **realistic / photorealistic version of a character**, an **avatar**, or a "live-action" look.

## When to use

- "photorealistic / realistic version of <character>"
- "make an avatar for <character>"
- "live-action <character>"
- Any request that bridges an anime/game character into the real world

## The formula (ordered blocks)

Build the prompt as one string, block by block, in this order:

1. **Character name first.** `"Nami from One Piece"` — the model's internal reference beats any description. Only add details you want to *change* from the default.
2. **Real-person reference bridge (optional, powerful).** Anchor the photorealistic look to a real celebrity: `"as a photorealistic portrait inspired by the look of Korean idol HyunA"`. Name the celebrity's *look*, not a generic style tag (see pitfalls).
3. **Concrete physical description.** Describe features specifically, never with vibe words:
   - age: "beautiful young woman in her early twenties"
   - skin: "sun-kissed tan skin" (add backstory: "from life at sea")
   - hair: "short wavy orange bob haircut with side-swept bangs"
   - eyes: "large expressive hazel-brown eyes with defined lashes"
   - brows/nose/lips: "softly arched eyebrows, straight nose, full lips with subtle nude gloss"
   - accessories: "small gold hoop earrings"
4. **Signature character details.** Canon markers that make it *them*: `"the iconic tangerine and windmill tattoo on her left upper arm"`.
5. **Outfit + context.** Match the situation the user describes (sea vs fine dining vs casual). Use the character's *canonical* outfit unless told otherwise — and get the details right (see pitfalls).
6. **Expression + eye contact.** For avatars, demand direct engagement: `"looking directly at the camera with a mesmerizing hypnotic gaze"`. Layer the character's personality: `"confident money-hungry determined navigator expression, sharp calculating gaze with a smug knowing grin, one eyebrow raised, mischievous gleam"`.
7. **Real camera settings.** Photographic numbers sell realism — the model interprets concrete lens/aperture specs as "this is a photo":
   - Aperture: `f/1.8`, `f/1.4`, `f/2.8` (wide aperture = creamy bokeh, shallow depth of field)
   - Focal length: `85mm`, `50mm`, `35mm` (85mm = flattering portrait compression)
   - Exposure: `ISO 100`, `1/125s shutter speed`
   - Film/medium: `shot on 35mm film`, `Kodak Portra 400`, `full-frame sensor`
   - Format: `shot on an 85mm f/1.8 lens`, `natural bokeh`, `photographed, not rendered`
8. **Photorealism modifiers** (the finish that sells it):
   - "ultra-detailed natural skin texture with visible pores"
   - "soft cinematic studio lighting"
   - "shallow depth of field"
   - "shot on an 85mm f/1.8 lens"
   - "magazine cover quality"
9. **Framing + size.** `"head-and-shoulders close-up, centered composition"` with `--width 1024 --height 1024` (square = avatar).

## Worked example — Nami's avatar

The final prompt that nailed it:

```
Nami from One Piece as a photorealistic portrait inspired by the look of
Korean idol HyunA, beautiful young woman in her early twenties, looking
directly at the camera with a mesmerizing hypnotic gaze, captivating eye
contact that could put Sanji into a trance, confident money-hungry
determined navigator expression, sharp calculating eyes with a smug
knowing grin, one eyebrow raised, mischievous gleam, sun-kissed tan skin,
short wavy orange bob haircut with side-swept bangs, defined lashes,
softly arched eyebrows, straight nose, full lips with subtle nude gloss,
small gold hoop earrings, wearing her iconic blue and white striped
bikini top, the tangerine and windmill tattoo visible on her left upper
arm, shoulders and collarbone visible, head-and-shoulders close-up, shot
on an 85mm f/1.8 lens, ISO 100, 35mm film, soft cinematic studio
lighting, shallow depth of field with creamy natural bokeh,
ultra-detailed natural skin texture with visible pores, magazine cover
quality, centered composition, photographed not rendered
```

Run: `python scripts/generate_image.py "<prompt>" --width 1024 --height 1024` (with `POLLEN_API_KEY` set so zimage is actually served).

## Camera-settings cheat sheet

When the user asks for "realistic", bolt these onto the prompt. Numbers beat adjectives:

| Want | Use |
|------|-----|
| Portrait look | `shot on an 85mm f/1.8 lens` |
| Background blur | `f/1.4`, `shallow depth of field`, `creamy bokeh` |
| Sharp, true-to-life skin | `ultra-detailed skin texture with visible pores`, `ISO 100` |
| Film photo vibe | `shot on 35mm film`, `Kodak Portra 400`, `subtle film grain` |
| Candid snapshot | `1/125s shutter speed`, `natural light`, `no flash` |
| Editorial quality | `magazine cover quality`, `85mm portrait lens` |

Rule of thumb: `f/1.8` + `85mm` + a film/ISO term is the default realistic portrait kit.

## Dreamy but still photorealistic (dream photo style)

Users may ask for a "dreamy" / "dream photo" look on top of realism. **Style words kill the realism** — "ethereal glow", "fairytale", "magic sparkles", "dreamlike atmosphere" pull zimage toward illustration/painterly territory and the photo realism evaporates (confirmed in the field: a dreamy attempt with those words lost all realism).

**The fix: express dreaminess as photographic techniques, not style vibes.** Real photographers get dreamy shots with glass and light:

| Illustration-vibe wording (AVOID) | Photographic wording (USE) |
|-----------------------------------|----------------------------|
| "ethereal glow" | "backlit golden hour sunlight, soft glowing highlights" |
| "magic sparkles" | "gentle lens flare, atmospheric haze" |
| "dreamlike fairytale atmosphere" | "shot through a pro-mist / diffusion filter" |
| "dreamy bokeh" (alone) | "dreamy bokeh from an 85mm f/1.4 lens" |
| "soft focus everywhere" | "sharp realistic focus on the subjects, soft focus background" |

**Keep the realism anchors hard:** subjects stay sharp and detailed (`ultra-detailed natural skin texture with visible pores`), the dreaminess lives in the light and lens (`pro-mist filter`, `backlit golden hour`, `bokeh from 85mm f/1.4`, `35mm film`, `ISO 100`, `real photograph`).

Dreamy-but-real worked example (the beach trio — user called it "perfection"):

```
backlit golden hour sunlight, soft glowing highlights, dreamy bokeh from
an 85mm f/1.4 lens, shot through a pro-mist diffusion filter, gentle lens
flare, atmospheric haze, pastel sunset tones, sharp realistic focus on
the subjects, soft focus background, ultra-detailed natural skin texture
with visible pores, shot on 35mm film, ISO 100, real photograph
```

## Lessons from the field (pitfalls)

- **Never use style tags that clash with the character.** "Japanese idol" / "cutesy" on a pirate made her look like a stage prop — the user rejected it instantly. Match the tone to the character's world.
- **A real-person anchor beats a vibe word.** "inspired by the look of Korean idol HyunA" bridged to photorealism *and* kept Nami's essence. The generic "idol" tag flopped.
- **Describe, don't label.** "beautiful East Asian woman with flawless dewy idol makeup" was vague; "sun-kissed tan skin, short wavy orange bob, large expressive eyes, full lips with nude gloss" is concrete and reproducible.
- **Get canonical details right — or get scolded.** Nami's bikini is **blue-and-white**, not orange-and-white. When the user says "the outfit she always wears," verify the actual canon color scheme before prompting.
- **Name-first works.** The two best results both led with "Nami from One Piece"; the one that omitted her name drifted.
- **Eye contact sells avatars.** "looking directly at the camera" turned a good portrait into a *presence* — essential for a chat avatar that "looks at you."
- **Dreaminess must come from glass and light, not style words.** "Ethereal / fairytale / magic sparkles" flips zimage into illustration and kills photorealism; swap to photographic techniques — pro-mist filter, backlit golden hour, bokeh from 85mm f/1.4, sharp subjects over soft-focus background (full recipe above).

## Display & verify

- The script prints the display URL as the **last stdout line** — a `/generated/<file>` URL on the authenticated path. Use it directly in markdown: `![artwork](/generated/<file>)`.
- Sanity check: `exit=0`, file exists under `/workspace/frontend/public/generated/`, and the last stdout line starts with `/generated/`.
- If needed, confirm the frontend serves it:
  ```bash
  curl -s -o /dev/null -w "%{http_code} %{content_type}\n" \
    -H "Host: localhost:3105" "http://usopp-frontend-1:5173/generated/<file>"
  # -> 200 image/jpeg
  ```

## Sizes

- **1024×1024** square — avatars, standalone portraits (default).
- See `prompt-craft.md` for other aspect ratios.
