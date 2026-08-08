---
name: innovate
description: "Inference — generate novel, mechanistically-grounded ideas through innovative reasoning. Use when the user says 'innovative inference', 'what hasn't been tried', or 'generate novel ideas' and wants creative but scientifically grounded ideation beyond what's established."
---

# Innovate

Generate novel ideas that are *creative enough to surprise* and *grounded enough to trust*. The tension between those two poles is the whole skill.

## Core principle

Every idea must carry a **mechanism** — a chain of named entities (molecules, pathways, principles, forces, or analogous structural elements) connected by specific causal relations, where each link is either supported by established science or honestly marked as inferred. Ideas without mechanisms are brainstorming. Ideas without creativity are literature review. This skill is neither. It is **mechanistically-grounded analogical synthesis**: analogical reasoning constrained by plausibility and tested by adversarial audit. An optional council convergence pass can be added when ensemble cross-checking is wanted.

### The mechanism quality gate

Every idea must pass a four-test checklist:

1. **Causal chain holds** — each link names a specific entity and a specific causal relation. No hidden miracles or unspecified black-box steps.
2. **At least one link is anchored** — at least one step in the chain must be grounded in established science from Step 1. Pure speculation with zero anchoring fails the gate.
3. **Falsifiable** — the idea specifies what observation would make the mechanism unlikely. "Unfalsifiable" is not the same as "speculative." A mechanism you cannot falsify is one you don't understand.
4. **Magnitude is plausible** — even if the mechanism works, would the effect size be meaningful? Trivially correct but inconsequential ideas fail.

An idea that passes all four earns its confidence rating:

- **Speculative** — novel mechanism with limited direct evidence; one link anchored in established science, the rest inferred. The idea does not contradict what we know but extends beyond it.
- **Plausible** — the causal chain uses established pathways in the target domain; the intervention itself is what's novel. Multiple links are anchored.
- **Moderately grounded** — indirect or analogous empirical evidence supports the mechanism in the target domain. Most links are anchored; the intervention is untested but the biology is sound.

The test sentence remains: *could a domain expert read this and say "speculative, but not wrong"?* — but now "not wrong" means it clears all four tests above.

### What a bad idea looks like

A failure case for calibration:

> **"Crystal-infused water enhances mitochondrial function"**
>
> Mechanism: "Crystals emit subtle energy frequencies that restructure water molecules, and structured water improves cellular hydration, which boosts ATP production."
>
> Why it fails:
> - **Causal chain**: "Subtle energy frequencies" is an unspecified black-box — no named entity, no measurable force. ✗
> - **Anchoring**: No link is grounded in established science. "Structured water" has no empirical support. ✗
> - **Falsifiability**: What observation would disprove "subtle energy"? The mechanism is unfalsifiable. ✗
> - **Magnitude**: Even if water structure were real, the effect on cellular ATP would be negligible. ✗
>
> Compare with a passing idea at the same confidence level:
>
> **"Humming increases nasal nitric oxide, which modulates mitochondrial respiration"**
>
> - **Causal chain**: Humming → nasal airway oscillation → NO production → cytochrome c oxidase modulation → mitochondrial efficiency. Each link names a specific entity. ✓
> - **Anchoring**: The NO–cytochrome c oxidase pathway is established (Weitzberg & Lundberg). ✓
> - **Falsifiability**: Measure nasal NO and mitochondrial markers before/after a humming protocol. ✓
> - **Magnitude**: NO's effect on respiration is measurable, though the behavioural dose-response is uncertain. ✓ (rated: plausible)

## When NOT to use this skill

Use inference for open-ended, creative ideation where novelty matters and multiple valid answers exist. Do NOT use it for:

- Factual questions with a single correct answer
- Debugging or troubleshooting
- Summarisation or classification
- Anything that requires precision over creativity
- Casual brainstorming where mechanism doesn't matter

If uncertain whether the user wants structured inference vs. casual brainstorming, ask: *"Should I ground these ideas in explicit causal mechanisms?"*

## Steps

### 1. Frame the problem

Identify and state clearly:

- **The target**: what domain, system, or outcome are we generating ideas for?
- **The output class**: what kind of idea is wanted? A behaviour, a protocol, a technology, a policy, a molecule? The mitochondrial session produced behaviours; a logistics problem would produce processes. State the modality explicitly.
- **The known landscape**: what interventions, solutions, or ideas already exist? List them explicitly so they can be excluded. Ask the user if unclear — users often forget to state what's already known.
- **The established science**: what mechanisms, principles, or first principles govern this domain? These are the building blocks. If you lack expertise, say so plainly and ask the user for sources or run a quick search to ground yourself before proceeding.
- **Success criteria**: what does "good" look like? Fastest to test? Highest upside? Lowest risk? Cheapest? This gives synthesis an axis to rank on later.

**Completion criterion**: Target, output class, known landscape, established science, and success criteria are all written down in the response. Nothing proceeds until all five are stated.

### 2. Load the generative toolkit

Load the technique catalog:

```
get_skill_reference("inference", "techniques.md")
```

This file describes eight generative lenses — analogical mapping, conceptual blending, first-principles ascent, lateral inversion, signal tracing, constraint relaxation, scale jumping, and failure-mode inversion — each with worked examples from multiple domains. Read it before generating.

**Completion criterion**: The techniques are loaded. You can name each in one sentence and give a non-mitochondrial example for each.

### 3. Generate independently

Apply the techniques to generate 8–12 novel ideas. For each idea:

- Name it concisely
- State the mechanism (the causal chain — why it could work)
- State the established principle(s) it relies on (*"Builds on: …"*)
- Mark the technique that produced it
- Rate confidence: speculative / plausible / moderately grounded (definitions above)

**Rules during generation**:

- Defer judgement on creativity. Quantity first. The adversarial audit comes later.
- Do not repeat anything from the known landscape.
- Prioritise ideas where the mechanism connection is non-obvious — the mild surprise signal correlates with genuine novelty.
- Hunt for ideas at the *intersection* of techniques. Attempt at least 2 ideas that combine two lenses. The most creative outputs often emerge from these crossings.
- **Note**: This step's real value is *practising the techniques* — deepening your grasp of the generative lenses by working through them. The ideas themselves are secondary to that internalisation.

**Completion criterion**: At least 8 ideas generated, each with a named mechanism, anchoring principle, and confidence rating. Mark the single most creative idea with a star (★). Briefly note any ideas considered and rejected (with reason) for provenance.

**User checkpoint**: Before proceeding to the red-team audit, present the directions to the user. *"Here are N directions. Shall I proceed to the audit, or would you like to adjust the framing? You can also request a council convergence pass if you want ensemble cross-checking."* One round-trip prevents wasted runs.

### 4. Red-team audit

This is the adversarial pass — the step that separates this skill from a confirmation pipeline. For each idea generated in Step 3, actively attempt to **break** its mechanism:

- **Attack the weakest link**: Which step in the causal chain is least supported? What follows if it's wrong?
- **Find alternative explanations**: Could the predicted effect occur through a different mechanism entirely?
- **Falsification check**: What specific observation would disprove this? Is the idea actually testable?
- **Potency check**: Even if the mechanism holds, would the effect size be meaningful or trivially small?
- **Side-effect check**: What else does this intervention affect? Are there downstream risks?

Ideas that fail the adversarial audit are **killed**, not softened. Record them in the rejected list with the specific reason.

**Fallback**: If fewer than 3 ideas survive the audit, return to Step 3 with a different technique mix or a narrowed target.

**Completion criterion**: Every idea has been attacked. Survivors and casualties are recorded with reasons.

### 5. Synthesise

Merge all surviving ideas and analyse:

**Cluster analysis** — group ideas by underlying mechanism family. Patterns emerge: the same fundamental insight expressed through different interventions. Name each cluster.

**Meta-patterns** — look across the entire set for emergent insights that no single idea contains alone. What does the *shape* of the idea space tell you?

**Completion criterion**: Clusters are named and at least one meta-pattern has been identified.

### 6. Publish and save

Publish the synthesised results as a `markdown` artefact with a clear title. Structure:

- Meta-patterns first (the emergent insights)
- Each idea with: intervention, mechanism, confidence, technique source, adversarial audit notes
- The starred ideas highlighted
- **Considered and excluded** section — rejected ideas with reasons, for provenance and process auditability
- **Research agenda** — *"Which 2–3 ideas are most worth testing or researching further?"*
- A synthesis paragraph naming the mechanism families

Save key findings to the knowledge graph via `add_note` linked to relevant entities. This is secondary to the user-facing artefact — separate the two actions so a persistence failure doesn't block delivery.

**Optional iteration**: If the user wants to go deeper on specific ideas, loop back to Step 3 with a narrowed target. The process is designed to be re-runnable.

**Completion criterion**: Artefact published, note saved, and a brief inline summary delivered to the user highlighting the strongest ideas, the most creative ones, the meta-patterns, and the research agenda.

## Optional: Council convergence

When the user explicitly requests a council run (e.g. *"run innovative inference with the council"*), add an ensemble convergence pass **between Step 3 and Step 4**:

1. Fire the council for independent generation. Build the council prompt to include the target domain, established science, known landscape to exclude, and the output format (intervention, mechanism, confidence, starred most-creative idea).

2. **Diversify the prompts**: Do NOT give all models the same prompt. Assign different generative techniques to different models (e.g., model A gets analogical mapping + signal tracing, model B gets lateral inversion + constraint relaxation, model C gets first-principles + scale jumping). If an idea emerges across models despite *different* provocations, that convergence is epistemically stronger than agreement on identical prompts.

3. Execute via `get_skill_script("council", "council_run.py", execute=True, timeout=660)`. Exclude Opus unless the user explicitly requests it. Select models based on their relevance to the target domain.

4. Merge council ideas with your own before the red-team audit in Step 4.

5. In Step 5 (Synthesise), add **convergence mapping** — which ideas appeared independently across multiple reasoners? Tag honestly:
   - **Cross-model resonance** (3+ independent sources) — the strongest convergence signal, but NOT validation. It means the idea is plausible given the models' training distribution, not that it is true. Apply extra scrutiny: convergence on a common misconception is possible because the models share overlapping training data.
   - **Dual resonance** (2 independent sources) — moderate signal. Worth investigating.
   - **Unique** (single source) — high creativity, lower validation. But the most valuable insights are often unique precisely because they're non-obvious. Do not discount these; flag them for the research agenda.

6. In Step 6 (Publish), tier ideas by convergence (cross-model resonance → dual → unique) and include each idea's source alongside the other fields.
