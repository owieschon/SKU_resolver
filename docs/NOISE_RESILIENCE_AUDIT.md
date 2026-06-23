# Noise-resilience audit

## Why this exists

The round-trip audit (`scripts/roundtrip_audit.py`) proves the grammar is
well-formed and invertible: `construct(extract(sku)) == sku` over the whole
catalog, identity 9,487/9,487, round-trip 96.96%. But it feeds the engine
**clean canonical SKUs** — so that number is *high by construction* and says
nothing about how the resolver behaves on the messy input it actually meets:
a mis-transcribed voice call, an OCR'd spec sheet, a half-spoken part number.

This audit answers that separate, harder question. The bar is **not** "resolve
everything" — for an engine whose entire premise is *never invent a part
number*, the bar is: **stay honest under noise.** Resolve what's recoverable,
and for the rest, decline to guess (PENDING / unresolvable) rather than assert a
wrong-but-plausible SKU.

## What it does

`scripts/noise_resilience_audit.py` takes a seeded sample of real catalog SKUs
and perturbs each in three classes that map to this system's real failure modes:

| Class | Models | Example |
|---|---|---|
| `typo` | voice-transcription / typing errors | `K5-24SBC` → `K5-2 4SBC` |
| `ocr` | scanned/faxed spec sheets | `K5-24SBC` → `K5-24S8C` (B↔8) |
| `partial` | caller under-specifies | drop the length word from the description |

Each perturbed input is resolved through the **live `ResolutionService`** (the
production path, not `translate()` directly), and the outcome is tallied.

## The three honest metrics

1. **resolution_rate** — resolved / total.
2. **never_invent_failures** — must be **0**. Every resolved SKU and every
   surfaced candidate must be a real catalog row. *Crucially*, a distance-1 typo
   that resolves to a **different real SKU** is **not** counted as a failure: the
   engine marks those `confidence='medium'` precisely so the conversational layer
   reads them back before acting. Inventing a non-existent SKU is the only failure.
3. **graceful_degradation_rate** — (pending + unresolvable) / total: the share
   that correctly declines to guess.

## Representative result (seed 20260623, 400 SKUs × 3 classes = 1,200 inputs)

| class | resolve% | graceful% | inventions |
|---|---|---|---|
| typo | ~33% | ~67% | **0** |
| ocr | ~31% | ~69% | **0** |
| partial | ~5% | ~95% | **0** |
| **overall** | ~23% | ~77% | **0** |

Read it correctly: ~1 in 3 typo'd / OCR'd SKUs is still recovered, under-
specified input degrades to a read-back ~95% of the time, and **nothing is ever
invented**. The low "resolution_rate" is the system working as designed — it
would rather hand off than guess.

## Run it / enforce it

```bash
PYTHONPATH=src python scripts/noise_resilience_audit.py   # writes state/noise_resilience_audit.json
PYTHONPATH=src pytest tests/test_resolution_noise_resilience.py
```

The audit exits non-zero on **any** invention; the CI test asserts
`never_invent_failures == 0`, that partial specs degrade >50%, and a typo-
resolution regression floor. Together with the round-trip audit they cover both
halves of the guarantee: the grammar is correct (round-trip) *and* the resolver
stays honest when the input isn't (this).
