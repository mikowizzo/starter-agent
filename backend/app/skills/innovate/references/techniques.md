# Inference Technique Catalog

Eight generative lenses for producing novel ideas. Each has worked examples from multiple domains to demonstrate the techniques' generality. Load this before generating.

## 1. Analogical Mapping

Take a structure, relationship, or dynamic from one domain and map it onto the target.

**How to apply**: Identify a *structural* parallel — a relationship pattern, an information flow, a feedback loop — that exists in a different field. Distinguish *surface* similarity (looks the same) from *structural* similarity (works the same). Only the latter produces trustworthy mechanisms. Then ask: if this structure exists there, what would the equivalent intervention be here?

**Worked example (biology → behaviour)**:
- Source: Mitochondria communicate via nanotunnels and exchange resources to rescue damaged neighbours.
- Target: Human behaviour.
- Map: If mitochondria need exchange to stay healthy, and humans are the "cells" of a social system, then humans need physiological synchrony (shared breathing, movement, rhythm) to exchange regulatory signals.
- Result: **Synchronised social entrainment** (choir, dancing, drumming).

**Worked example (architecture → software)**:
- Source: Gothic cathedrals use flying buttresses to redistribute lateral forces, allowing thinner walls and more light.
- Target: Software architecture.
- Map: If external supports carry load that would otherwise break the system, then an external service bus or sidecar pattern can carry cross-cutting concerns (logging, auth) that would otherwise bloat core services.
- Result: **Sidecar pattern as architectural buttress**.

**Where to hunt analogies**: ecosystems, physics, economics, linguistics, computation, music, architecture, cooking, warfare, agriculture, city planning.

## 2. Conceptual Blending

Merge two distinct conceptual spaces to produce a hybrid that has properties of both but belongs to neither.

**How to apply**: Take an established practice from domain A and an established mechanism from domain B. Force them together. What practice would satisfy both?

**Worked example (exercise science + olfactory neuroscience)**:
- Space A: Hormetic stress triggers adaptation (AMPK, PGC-1α pathways).
- Space B: The olfactory epithelium is a site of adult neurogenesis (metabolically expensive, mitochondria-intensive).
- Blend: If neurogenesis is metabolically expensive and hormetic stress triggers adaptation, then deliberately stressing the olfactory system with varied scents is a form of "exercise" for smell.
- Result: **Olfactory workouts** — focused sniffing at varied scents, rotated daily.

**Worked example (finance + ecology)**:
- Space A: Options pricing — the value of maintaining flexibility to act when conditions change.
- Space B: Ecological succession — pioneer species prepare the ground for climax communities.
- Blend: If maintaining flexibility is valuable and early actors shape the environment for later ones, then deliberately introducing low-cost "pioneer" interventions that increase future optionality is a strategy.
- Result: **Optionality planting** — seeding small, reversible experiments that expand the space of future moves.

## 3. First-Principles Ascent

Start from an established mechanism and build upward, asking "what else follows from this?" — not "what else is similar?"

**How to apply**: Take a first principle. Ask: if this mechanism is real and important, what downstream consequences should we see? What behaviours would optimise for it? What would exploit it?

**Worked example (circadian biology)**:
- Principle: Mitochondria have their own circadian clocks; behavioural timing matters.
- Ascent: If mitochondrial function oscillates throughout the day, there are optimal windows for energy-intensive activities. But also — disrupting the clock (even with "good" behaviours at the wrong time) causes damage.
- Result: **Circadian-aligned behavioural timing** — not just sleep, but eating, exercising, and light exposure timed to cellular rhythms.

**Worked example (thermodynamics → data centres)**:
- Principle: Every bit of computation produces waste heat (Landauer's principle).
- Ascent: If computation is fundamentally thermodynamic, then the heat isn't a bug — it's a feature. What if we used the waste heat productively?
- Result: **Computational district heating** — data centres sited to heat buildings, turning waste into output.

## 4. Lateral Inversion

Identify the dominant assumption in the field. Invert it. What would follow if the inversion were true?

**How to apply**: State the conventional wisdom as a single sentence. Negate it. Build from there. This technique is most powerful when the inversion is *genuinely* counterintuitive — not just a known alternative.

**Worked example (consistency → variability)**:
- Conventional wisdom: Consistency is the key — do the same thing every day.
- Inversion: What if variability itself is the intervention? What if the system habituates to repeated identical signals and stops responding?
- Result: **Variation as the meta-intervention** — rotating postures, temperatures, sounds, and stimuli rather than optimising any single one.

**Worked example (centralisation → edge autonomy)**:
- Conventional wisdom: Centralised coordination is more efficient.
- Inversion: What if centralisation is the bottleneck? What if edge nodes making autonomous decisions based on local signals produce more resilient systems?
- Result: **Stigmergic coordination** — agents that coordinate through environmental signals rather than central commands (like ants via pheromone trails).

## 5. Signal Tracing

Follow a specific signalling molecule, pathway, or mechanism to its behavioural implications.

**How to apply**: Identify a key molecule or pathway. Trace it: Where does it come from? What triggers its release? What does it do downstream? What human behaviour would modulate it?

**Worked example (nitric oxide)**:
- Molecule: Nitric oxide (NO).
- Trace: NO modulates mitochondrial respiration via cytochrome c oxidase. Nasal breathing produces NO in the sinuses. Humming dramatically increases nasal NO production (Weitzberg & Lundberg).
- Behavioural implication: If nasal NO benefits mitochondrial efficiency, and humming increases it, then humming is a mitochondrial intervention.
- Result: **Humming / vocal toning protocol**.

**Worked example (supply chain → economics)**:
- Signal: Lithium price.
- Trace: Lithium price → battery manufacturing cost → EV adoption curve → grid-scale storage economics → renewable energy deployment rate.
- Behavioural implication: If lithium price is the leverage point, then interventions that decouple battery cost from lithium (alternative chemistries, recycling) have outsised downstream effects.
- Result: **Chokepoint decoupling strategies**.

## 6. Constraint Relaxation

Identify the rate-limiting step, bottleneck, or binding constraint in the current system. Temporarily imagine it removed. What becomes possible?

**How to apply**: Find the one thing that everyone agrees is the hard part — the bottleneck everyone complains about. Now ask: if that constraint didn't exist, what interventions would suddenly be viable? The mechanism flows naturally — you explain what removing the constraint unleashes.

**Worked example (energy budget model)**:
- Constraint: The brain has a fixed daily energy budget; high cognitive load depletes it.
- Relax: If the energy budget could be temporarily expanded, what would you do with the surplus?
- Result: **Energy budget cycling** — structuring the day to alternate high-demand cognitive work with deliberate recovery periods, rather than assuming steady-state capacity.

**Worked example (software → engineering)**:
- Constraint: Network latency is the hard limit on real-time collaboration tools.
- Relax: If latency were zero, what collaboration patterns become possible?
- Result: **Speculative execution for UI** — predicting and pre-rendering collaborator actions so the interface feels instant regardless of latency.

## 7. Scale Jumping

Transpose a mechanism, pattern, or solution across levels of organisation. What works at one scale may have an analogue at another.

**How to apply**: Identify the current scale of the problem (molecular, cellular, individual, group, societal, planetary). Jump one or more levels up or down. Ask: does this mechanism have a counterpart at a different scale?

**Worked example (cellular → societal)**:
- Mechanism: Mitochondria adapt to stress through hormesis (moderate stress → strengthening).
- Scale jump: If hormesis operates at the cellular level, does it operate at the societal level? Do communities that face moderate, recoverable challenges become more resilient than those shielded from all stress?
- Result: **Societal antifragility design** — deliberately introducing manageable stressors into systems (stress-testing, red-teaming, fire drills) to build adaptive capacity.

**Worked example (software → economics)**:
- Mechanism: Microservices decompose a monolith into independently deployable components.
- Scale jump: If decomposition works for software, does it work for organisations? What would a "microservice organisation" look like?
- Result: **Decentralised autonomous teams** — small, independent units with clear APIs (contracts) to other teams, deployable and replaceable independently.

## 8. Failure-Mode Inversion

Instead of asking "what would make this work?", ask "what would guarantee failure?" Then design interventions that specifically neutralise those failure modes.

**How to apply**: Define the target. Systematically enumerate what would make it fail catastrophically — not just "not work," but actively break. Then invert each failure mode into a design constraint or intervention.

**Worked example (mitochondrial dysfunction)**:
- Failure mode: Constant, unvarying metabolic demand causes mitochondrial stagnation.
- Invert: What prevents stagnation? Variable, unpredictable demand.
- Result: **Metabolic unpredictability protocols** — introducing random variation in energy demand (stochastic exercise intervals, varied fasting windows) to prevent adaptation-induced stagnation.

**Worked example (team performance)**:
- Failure mode: Groupthink kills creativity — teams converge on safe, consensus answers.
- Invert: What prevents groupthink? Structural incentives to disagree.
- Result: **Designated dissenter protocol** — assigning a rotating team member to argue against the dominant proposal before any decision is finalised.

---

## Combination Strategies

The most creative ideas often emerge from combining techniques. Attempt at least 2–3 ideas that live at an intersection:

- **Analogical mapping + signal tracing**: Find a structural analogy, then trace the actual mechanism that would make it work.
- **Lateral inversion + first-principles ascent**: Invert the assumption, then build upward from the inverted principle.
- **Constraint relaxation + scale jumping**: Remove a bottleneck, then transpose the freed-up mechanism to a different scale.
- **Failure-mode inversion + conceptual blending**: Find the failure mode in domain A, blend it with a protective mechanism from domain B.

**Choosing techniques**: Don't cycle through all eight ritually. Favour techniques based on the problem shape:
- *Well-understood mechanism, unknown applications* → signal tracing, first-principles ascent
- *Stuck field with entrenched assumptions* → lateral inversion, constraint relaxation
- *Rich cross-domain parallels available* → analogical mapping, conceptual blending
- *Need to see the system differently* → scale jumping, failure-mode inversion

Don't force a single technique per idea. Let them interact.
