# Kavach Knowledge Base v1.0 Seed

This is a deliberately small, evidence-backed hackathon knowledge base.

## Architecture

- entities: genes, variants, phenotypes, mechanisms, populations, drugs
- relationships: drug ↔ gene/variant ↔ phenotype ↔ clinical outcome
- population_frequencies: population observations with source metadata
- sources: authoritative evidence references
- rules: deterministic reasoning constraints
- validation_cases: known drugs used to backtest the same engine used for new-drug simulation

## Important scientific guardrails

1. Population frequency is NOT probability of harm.
2. A mechanistic precedent for a hypothetical drug is NOT proof that the hypothetical drug causes the same clinical outcome.
3. Unknown relationships must return INSUFFICIENT_EVIDENCE.
4. Weak evidence must not automatically become a high/critical result.
5. The LLM may explain structured engine output but must not create new medical facts.

## What is intentionally incomplete

The four documented cases are the initial validated seed. CYP2D6 and CYP2C19 population-frequency coverage should be expanded from their CPIC frequency tables before treating numerical frequency scoring as comprehensive. The current dataset therefore uses verified frequency observations where directly supported and leaves the rest to future expansion.

## Recommended next implementation step

Build a deterministic loader + resolver over these files, then implement:
- direct documented-case matching
- mechanistic-precedent matching
- population relevance
- trial representation gap
- evidence ceiling
- validation/backtest mode

Do not ask an LLM to populate or modify this knowledge base automatically.
