# Prompt Craft

Vocabulary for image-generation prompts. A great prompt names a subject in a medium under lighting in a composition conveying a mood.

## Structure

```
[subject] + [medium] + [style] + [lighting] + [composition] + [mood] + [detail modifiers]
```

Lead with the subject, then layer medium and style, then the light and framing that give it depth. 3–6 phrases is the sweet spot. Always include subject and medium; add at least two of {style, lighting, composition, mood}.

## Z-Image prompt rules (model-specific, important)

zimage (the only generate model) behaves differently from models that support negative prompts:

- **Negative prompts do NOT work.** Phrases like "no text", "without watermark", "no people", "not cartoonish" are ignored — zimage has no negative-prompt channel. Do not waste prompt tokens on them.
- **Everything you describe positively tends to appear.** zimage is very sensitive to positive prompts — if a concept is in the prompt, expect it in the image. This is a feature: list what you WANT; never describe what you DON'T want.
- **Avoid accidental inclusions.** Since negatives are ignored, "a beach with no umbrellas" risks painting umbrellas. Say only what should exist: "an empty beach".
- **Write only positive, additive descriptions.** State desired elements, style, lighting, and mood; assume any unmentioned detail is up to the model.

## Mediums

### Watercolour
wet-on-wet, gouache, dry brush. Modifiers: soft edges, paper texture, pigment pooling. Styles: loose, botanical, urban sketch, sumi-e.

### Ink & Pen
ink wash, cross-hatching, line art, calligraphic. Modifiers: bold strokes, minimal, dynamic line weight. Styles: manga, sumi-e, expressive.

### Oil & Acrylic
oil painting, impasto, palette knife. Modifiers: thick texture, visible brushwork, luminous layers. Styles: impressionist, classical realism, baroque.

### Digital
digital painting, concept art, pixel art, 3D render. Modifiers: clean lines, vibrant, atmospheric. Styles: studio quality, cel-shaded.

### Photography
35mm, macro, long exposure, aerial. Modifiers: bokeh, depth of field, film grain. Styles: natural light, sharp focus.

## Style (cross-medium)

Layer these onto any medium to set the overall look:

- **Painterly** — visible brush marks, expressive colour
- **Photorealistic** — sharp, true-to-life, detailed
- **Stylised** — exaggerated forms, bold colours
- **Minimalist** — sparse, essential elements only
- **Ornate** — intricate detail, decorative richness

## Lighting
golden hour, soft diffused, chiaroscuro, rim lighting, dappled sunlight, moonlight, candlelit, overcast, studio, Rembrandt, volumetric rays, bioluminescent glow

## Composition
rule of thirds, centred, symmetrical, wide-angle, close-up, bird's eye, worm's eye, dutch angle, panoramic, negative space, leading lines, frame within a frame

## Mood
serene, melancholic, joyful, mysterious, ethereal, whimsical, dramatic, nostalgic, peaceful, vibrant, sombre, dreamlike, contemplative, cosy

## Aspect ratios
- **Square** 1024×1024 — portraits, standalone subjects
- **Landscape** 1280×768 — scenes, wide compositions
- **Portrait** 768×1024 — figures, vertical subjects
- **Panoramic** 1536×640 — sweeping vistas

