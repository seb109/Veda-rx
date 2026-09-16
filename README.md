# PGx Risk Radar

**Same drug. Different genome. Different outcome.**

Most drug safety data comes from a narrow slice of the world's genomes. This tool
shows which populations carry documented, elevated pharmacogenomic risk for a
given drug — and, for a candidate that has never been trialed, *derives* which
populations would show up as at-risk before a single participant is enrolled.

---

## The architecture claim

> **The knowledge base decides everything. The LLM only rephrases.**

Five normalised JSON tables hold the facts. Plain Python arithmetic derives the
risk from them. Groq is handed already-decided facts and asked to write two
sentences of prose. It is never in the decision path.

**How to prove it on stage,** in one move:

```bash
unset GROQ_API_KEY
uvicorn main:app --port 8000
```

Everything still works. Every response's `source` field reads
`knowledge_base` instead of `knowledge_base + llm_phrasing`. The
`/match` endpoint never calls the LLM at all, under any configuration.

That is the answer to *"how do you know it isn't hallucinating?"* — not an
explanation, a demonstration.

---

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # optional — put your Groq key here, never in source
uvicorn main:app --reload --port 8000
```

Open <http://localhost:8000>.

Running without a key is a supported, first-class mode — not a degraded one.

### Sanity check

```bash
python validate.py            # knowledge base integrity + sourcing discipline
curl localhost:8000/health    # provenance receipt: how much of the base is sourced
```

`main.py` **refuses to boot** on a broken foreign key. The original build shipped
a key mismatch (`HLA_1502` vs `HLA-B_1502`) that silently routed two of seven
pathways to a generic fallback, and nothing noticed. Now it can't start.

---

## Files

```
main.py                 FastAPI backend — loads the 5 tables, derives risk
pgx_risk_radar.html     frontend  ⚠️ NOT YET MIGRATED — see Current state
validate.py             knowledge base checks; run after any data edit
requirements.txt
.env.example

genes.json              7   gene-level facts + match_on (simulator entry conditions)
variants.json          18   specific alleles / SNPs, with functional status
phenotypes.json        10   clinical consequences + severity weights
populations.json       10   biogeographic groups + map coords + CPIC mapping
frequencies.json       38   the join table: (subject × population) → frequency + source
rules.json              6   documented drug cases (drives /explain)
```

---

## API

| Method | Path | What it does |
|---|---|---|
| `GET` | `/` | Serves the frontend |
| `GET` | `/health` | Status **and** provenance: row counts, confidence split, unverified row ids, outstanding data gaps |
| `GET` | `/populations` | Map geometry and labels. Replaces the frontend's hardcoded `CLUSTERS` |
| `GET` | `/phenotypes` | Dropdown source of truth. **The frontend must not mirror this** — a duplicated copy is what hid the HLA key mismatch |
| `GET` | `/genes` | Gene table plus the `match_on` contract, so a client can build the matcher form from the knowledge base |
| `POST` | `/explain` | Documented cases from `rules.json`. Unchanged request contract |
| `POST` | `/predict` | Pre-trial screen for one known pathway → full ranked population table |
| `POST` | `/match` | **The inversion.** Describe the candidate, get the pathways back. No gene input, no LLM |
| `POST` | `/brief` | Pre-trial genomic risk brief: enrollment targets + screening recommendations |

### `/match` — the core feature

The old simulator was circular: you picked the gene, and the gene *was* the
answer. Nothing was simulated. Now you describe what a chemist already knows:

```bash
curl -X POST localhost:8000/match -H 'Content-Type: application/json' -d '{
  "drug_name": "Compound X-104",
  "profile": { "prodrug": true, "activating_enzyme": "CYP2C19" }
}'
```

→ derives `CYP2C19` → flags `CYP2C19_PM` → ranks Pacific Islands (up to 70%)
above East Asia (~13.8%). **The gene was never supplied.**

Profile fields: `prodrug`, `activating_enzyme`, `clearing_enzyme`,
`structural_class`, `target_pathway`, `narrow_therapeutic_index`,
`immune_mediated_hypersensitivity_risk`, `dosing_algorithm_derived_from`.

Matching is a dict comparison against each gene's `match_on` block — clauses
ANDed, list values meaning "any of". A primary match scores 1.0, a
`secondary_match_on` match 0.6. An empty profile matches nothing and says so,
which is a real answer rather than an error.

Two genes are deliberately unreachable through enzyme questions alone:
`VKORC1` matches only on `target_pathway` (it is the drug's *target*, not a
metaboliser) and `HLA-B` only on structural class and immune risk. A screen that
asks only about metabolism misses both — roughly the mistake the field made for
twenty years.

---

## The knowledge base

```
variants.gene                   → genes.symbol
phenotypes.gene                 → genes.symbol
phenotypes.defining_variants[]  → variants.id
frequencies.population_id       → populations.id
frequencies.subject_id          → variants.id    (subject_type = "variant")
                                → phenotypes.id  (subject_type = "phenotype")
```

Each split exists because the old flat file was forcing a wrong answer:

- **genes vs variants** — one gene carries alleles with *opposite* effects.
  `CYP2C19*2` abolishes function; `*17` increases it. One row per gene could only
  tell the loss-of-function half of the story.
- **variants vs phenotypes** — phenotype is the consequence of a variant *pair*,
  and clinical risk is defined at the phenotype level. Two decreased-function
  alleles make a poor metabolizer; one makes an intermediate one.
- **frequencies as a join table** — frequency is the only genuinely contested,
  cohort-dependent thing here, so every observation carries its own source,
  confidence and cohort label. Embedding one number per gene forced the base to
  pick a winner per pathway and discard the range.

### Frequency resolution

1. A direct row where `subject_type = "phenotype"`.
2. Otherwise fall back through the phenotype's `defining_variants`.
3. If neither resolves → `risk_score: null` and the UI shows
   *"frequency not yet sourced"*.

**Step 3 matters.** One row (`rs12777823`, the African-ancestry warfarin variant)
has a deliberately null frequency. Scoring it as zero would render the
dosing-algorithm blind-spot case as *no risk* — inverting the exact point it
exists to make. Never substitute zero for null.

Multiple sourced observations for one population are **kept, not collapsed**. The
highest-scoring becomes the primary row; the rest nest under
`other_observations` with an `observation_range_display`. HLA-B\*15:02 in the
`hanchina` cluster spans 3.59%–20.58% across three cohorts, and flattening that
to one number is the error this whole base exists to prevent.

### Derived values

Three things are computed, never stored, and always labelled:

**Carrier frequency** from a sourced allele frequency, under Hardy-Weinberg:

```
carrier = 1 - (1 - allele_frequency)²
```

An 11% allele frequency means ~21% of *people* are carriers — nearly double. The
displayed number is always the number the score was computed from, with the
sourced allele frequency kept separately in `source_allele_frequency_display`.

**Poor-metabolizer phenotype frequency** from no-function allele frequencies. These
**add** (they are alternatives at the same locus) and the phenotype is `q²`:

```
q = Σ no_function_allele_frequencies      phenotype = q²
```

CYP2C19 `*2` (30.26%) + `*3` (6.89%) in East Asians → q = 37.15% → **13.8% PM**.
Published East Asian CYP2C19 PM rates are ~12–15%. Europe derives to ~2.2%
against a published ~2–3%. The aggregation validates independently against the
literature — worth saying if anyone challenges the arithmetic.

**Risk score**, 0–100:

```
risk = frequency × severity_weight / 5 × 100
```

### Allele-level specificity

A gene match is not a phenotype match. HLA-B\*15:02 and HLA-B\*57:01 sit on the
same gene but are triggered by entirely different drug classes. Without the
`trigger_structural_classes` filter, an aromatic anticonvulsant flags *abacavir*
hypersensitivity — and, in testing, ranked it **above** the phenotype that
actually applied. Excluded phenotypes are returned in
`excluded_by_allele_specificity` with the reason, rather than silently dropped.

---

## What's sourced and what isn't

36 of 38 frequency rows trace to a named reference. `/health` and `validate.py`
both report the live split; don't trust a number hardcoded in prose.

**Design choices, not citations** — all flagged in the data, and the API returns
`score_basis` on every scored row so a caller can't forget:

- `severity_weight` (1–5) is **our own ordinal scale**. Defined in
  `phenotypes.json` → `_meta.severity_weight_scale`. If asked, say it's your scale
  and point at the meta block.
- `approx_population_millions` is order-of-magnitude only.
- `typical_phase3_cohort_share` is mostly `null`, with one `unverified`
  placeholder.

**Two unverified rows**, both listed by `/health`:

- `freq_cyp2c19_2_eur` — European CYP2C19\*2 at ~15%. The Fricke-Galindo paper
  has the real value; this build just didn't extract it.
- `freq_cyp2c_rs12777823_americas_african_ancestry` — frequency null pending
  Perera et al. 2013 or gnomAD AFR.

Seven rows carry `action_required` notes. `validate.py` prints them as a
pre-demo checklist. The most substantive: the `hanchina` and `southasia`
clusters each span internal ranges too wide for one cluster and should be split.

### Corrections to the original knowledge base

1. **HLA-B\*57:01 was mislabelled.** The old base said `"~11% carrier frequency"`.
   CPIC reports 11.0% as an **allele** frequency — carrier is ~21%. The original
   understated the at-risk share of people by about half.
2. **`rs12777823`'s `stat` read `"N/A"`** with a label describing the *algorithm*,
   not a frequency. True statement, wrong field.
3. **CYP2D6 29% is Ethiopian**, from one small 1996 cohort — not North
   Africa-wide as the map label claimed.
4. **NAT2 53% is from a TB *patient* cohort**, likely enriched for slow
   acetylators, so it overstates the population rate.
5. **"Han Chinese" is not one frequency** — see above.
6. **Two populations added** (`africa_subsaharan`, `americas_admixed`) because
   1000 Genomes AFR is continental African and is *not* African-ancestry
   populations in the Americas. Conflating them is precisely the error the
   warfarin case is about.

Also added: `CYP2C19_UM` (the gain-of-function mirror, clustering in
Mediterranean and Middle Eastern populations) and `CYP2D6_PM` with its
**European** frequency as the highest in the base. Both exist to show the tool
measures genomic difference rather than ranking populations by risk — useful if
anyone asks whether the framing is loaded.

---

## Current state

**Backend: done and tested.** All nine endpoints verified with `GROQ_API_KEY`
unset. Integrity gate passes. Bad input (unknown drug, unknown phenotype id,
empty profile) returns a structured `found: false` / `matched: false` rather than
a 500.

**Frontend: not yet migrated.** `pgx_risk_radar.html` is still the pre-refactor
version. Consequences right now:

- *Documented cases* mode works — it has static fallback text and `/explain`'s
  request contract is unchanged.
- *Simulate a new drug* mode **will fall back to generic static text**, because it
  posts `{drug_name, gene}` and `/predict` now expects `{drug_name, phenotype_id}`.

To finish it:

1. Delete the hardcoded `CLUSTERS` and `GENES` objects; fetch `/populations` and
   `/phenotypes` instead. The duplication is what hid the original key mismatch.
2. Point simulate mode at `/match` (drug-property form) with `/predict` as the
   single-pathway path.
3. Render the ranked population table, not one card — `frequency_display`,
   `measure`, `confidence` and `source` per row.
4. **Display the `source` field.** It's been returned all along and never shown.
   Surfacing it is what makes the architecture visible instead of merely true.
5. Fix `geneKey.replace('_', ' ')` → `replaceAll` (only replaces the first
   underscore today).

---

## Security

The Groq API key was previously hardcoded in `main.py`'s docstring. `.gitignore`
covers `.env` but not source files, so it was committed. **That key is burned —
rotate it** at console.groq.com.

The key now loads only from the environment or `.env`. `.env.example` is the
template; it holds no secret.

Before any real deployment: CORS is `allow_origins=["*"]`, and `/explain`,
`/predict`, `/match` and `/brief` are unauthenticated POST endpoints sitting in
front of a billable API. Fine for a hackathon, not for the internet.

---

## Not medical advice

Predicted signals are pathway-inherited inferences for pre-trial screening
prioritisation — not confirmed outcomes for any specific candidate, and not
clinical guidance. Documented cases cite real literature; check the `source_url`
on any row before repeating its number.
