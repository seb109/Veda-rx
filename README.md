# Veda-rx — PGx Risk Radar

**Same drug. Different genome. Different outcome.**

Most drug safety data comes from a narrow slice of the world's genomes. Dosing
tables, screening thresholds and label recommendations were largely derived in
European and East Asian cohorts. When a variant is common somewhere those cohorts
didn't sample, the resulting guidance is quietly wrong for everyone who carries
it — and nothing in the guidance says so.

Veda-rx makes that visible in two directions. For a drug with a documented
pharmacogenomic relationship, it reports which populations carry elevated risk
and on what evidence. For a candidate compound with no documented relationship of
its own, it reports the mechanistic precedent it would inherit from other drugs
on the same pathway — clearly labelled as a screening signal rather than a
finding.

---

## The design principle

**The knowledge base decides everything. The LLM only rephrases.**

A relational knowledge base holds the facts. A deterministic resolver derives
every returned field from them by fixed, auditable rules. Groq receives facts
that have already been decided and writes prose about them. It never selects a
gene, a population, a severity or a priority.

`resolver.py` contains no LLM calls and no network calls at all. Every field it
returns is either copied verbatim from the knowledge base or computed by a rule
you can read. With no API key configured the system behaves identically except
that explanations come from `build_deterministic_explanation` instead of Groq,
and responses report that in `explanation_source`.

This is what makes *"how do you know it isn't hallucinating"* a question with a
demonstrable answer rather than a reassuring one: the language model can be
removed entirely and the output is the same apart from prose style.

---

## Two things the resolver deliberately refuses to do

These are the most important design decisions in the project, and both are
refusals.

**It never converts population frequency into a probability of harm.** An 88%
carrier frequency is not an 88% chance of injury. Those are different quantities,
and multiplying frequency by severity produces a number that reads like a risk
probability and isn't one. Frequency observations are returned as *presentation
evidence* — self-labelled with their own `frequency_type`, `frequency_min`,
`frequency_max` and unit — and priority is derived separately from severity and
evidence level. `validate_resolver.py` asserts that no "probability" terminology
leaks into frequency output.

**It never infers a phenotype frequency from allele frequencies.** For a
gene-level relationship, `_population_signals_for` surfaces documented
per-allele frequency rows for every variant belonging to that gene, each tagged
with its own variant id. It does not combine them into a metabolizer-phenotype
frequency. Combining them would require assuming the variant table lists the
*complete* set of no-function alleles, that Hardy-Weinberg holds, and — for
CYP2D6 — would be simply wrong, because CYP2D6 phenotype is diplotype and
activity-score based rather than the square of a summed allele frequency. The
resolver declines to guess.

---

## How a result is graded

Every resolution carries an explicit status:

| Status | Meaning |
|---|---|
| `DIRECT` | A documented relationship exists between this drug and this gene or variant |
| `PRECEDENT` | This drug has no documented relationship, but the pathway does — borrowed from another drug sharing the mechanism |
| `INSUFFICIENT_EVIDENCE` | No supported relationship was found, or the drug/pathway isn't in the base |

Status is a first-class field, not a caveat appended to prose, so a precedent
cannot be rendered as a documented fact by a careless client.

Priority is then derived from severity and evidence level, and capped twice.

The **evidence ceiling** means anything short of `strong` evidence cannot report
a priority above `MODERATE`, however severe the documented clinical outcome is.
An unrecognised or missing evidence level is treated as weak, not strong — the
conservative default. `CRITICAL` is therefore reachable only when severity is
critical *and* evidence is strong.

The **precedent ceiling** caps any `PRECEDENT` result at `MODERATE` even when the
source relationship is itself critical and strongly evidenced. A mechanistic
precedent is unproven for the candidate drug by definition, and the priority
field says so structurally rather than relying on a reader noticing a footnote.

## Representation gaps

Given a set of planned trial regions, the resolver reports which populations have
a documented signal on the matched pathway but fall outside those regions. That
comparison is the project's actual thesis made operational: not "this population
is at risk" but "this population is at risk *and you were not planning to enrol
them*."

---

## Layout

| File | Role |
|---|---|
| `main.py` | FastAPI app. Serves the frontend and both API generations |
| `kb_loader.py` | Read-only loader for the relational KB. Fails loudly on missing or malformed files |
| `resolver.py` | Deterministic resolution and grading. No LLM, no network |
| `validate_resolver.py` | Test suite over loader and resolver; exits non-zero on failure |
| `kavach_knowledge_base_v1/` | The relational knowledge base (eleven tables) |
| `pgx_risk_radar.html` | Frontend |
| `rules.json`, `genes.json` | Legacy v1 data, still serving the v1 endpoints |

The knowledge base tables are `genes`, `variants`, `phenotypes`, `mechanisms`,
`populations`, `drugs`, `relationships`, `population_frequencies`, `sources`,
`rules` and `validation_cases`. `relationships` is the join table carrying the
drug↔gene↔variant↔phenotype edge along with severity, evidence level, clinical
outcome and `source_ids`. `sources` is separately normalised so every claim
resolves to an organisation, title and URL rather than an embedded string.
`validation_cases` is a test fixture living inside the KB, which is what lets
`validate_resolver.py` check resolution against expected relationship ids rather
than against hand-written assertions.

The loader distinguishes failure modes deliberately: a missing file, malformed
JSON, a missing `id` or a duplicate `id` raises `KBLoadError`, while a reference
to an unknown gene, variant, phenotype or source is collected into
`kb.warnings`. The intent is that structural breakage stops the process while a
KB mid-extension can still load.

## API generations

The v1 endpoints — `/explain`, `/predict`, `/genes` — read the flat root-level
`rules.json` and `genes.json` and predate the relational base. The v2 endpoints —
`/v2/explain`, `/v2/predict` — go through `kb_loader` and `resolver`, and accept
`planned_trial_regions` for gap detection. If the KB fails to load, `KB` is
`None`, `KB_LOAD_ERROR` holds the message, and `_require_kb()` returns HTTP 503
rather than letting v2 answer from nothing.

Both generations are live because the frontend is mid-migration: *simulate a new
drug* mode calls `/v2/predict` and renders `status`, `priority`,
`population_signals`, `representation_gaps` and `sources`, while *documented
cases* mode still calls v1 `/explain`.

---

## Known issues

Listed because a README that hides them is worth less than one that doesn't.

**`requirements.txt` omits `python-dotenv`.** `main.py` imports `load_dotenv` at
module level, so a clean clone fails at import before serving anything. The file
currently lists only fastapi, uvicorn, httpx and pydantic.

**The Groq API key was committed in an earlier revision** of `main.py`'s
docstring. Removing it from the current file does not remove it from git history,
so that key should be treated as public and rotated.

**KB warnings are invisible at runtime.** `/health` returns only `{status,
groq_configured}` — no row counts, no resolved KB path, no `KB_LOAD_ERROR`, no
warning count. A relationship pointing at a nonexistent variant therefore
degrades resolution silently, which is the same failure mode as the
`HLA_1502` / `HLA-B_1502` key mismatch that this architecture was partly built to
prevent.

**Representation gaps under-report.** `_representation_gaps` evaluates
`freq.get("frequency_max") or 0`, so a population whose signal is documented but
unquantified becomes zero, fails the `> 0` test, and drops out of the gap list.
The list is also not deduplicated, so a population with signals on several
variants appears more than once. And `planned_trial_regions` is matched by
uppercased string comparison with no validation against `kb.populations`, so a
typo silently counts as "not covered" and inflates the result.

**`_best_relationship` breaks ties on file order.** Two relationships resolving
to the same priority are separated by their position in `relationships.json`.

**Gene-level signals mix risk directions.** A CYP2D6 query returns
poor-metabolizer alleles (`*4`, `*10`, `*17`) in the same flat list as the
duplication alleles that cause ultrarapid metabolism — overdose risk and
treatment-failure risk presented identically. This is documented in the
resolver's docstring and currently asserted by the test suite.

**Four data files at the repository root are loaded by nothing.**
`variants.json`, `phenotypes.json`, `populations.json` and `frequencies.json`
belong to an earlier normalisation pass. Their `gene` foreign keys (`HLA-B`,
`CYP2C9`, `CYP2C_CLUSTER`) do not resolve against the flat `genes.json` beside
them, and nothing detects this because nothing reads them. The 36 sourced
frequency rows in `frequencies.json` are worth porting into
`population_frequencies.json` and `sources.json`; the files themselves are not
worth keeping, since diverging copies of the same data are what caused the
original bug.

**v1 and v2 can disagree.** The same drug through `/explain` and `/v2/explain`
draws on different data with no reconciliation, so severity, region and
statistics can differ between them.

**The frontend duplicates KB data.** `CLUSTERS`, `DRUGS`, `SIM_MECHANISMS`,
`SIM_PATHWAYS` and `SIM_POPULATIONS` are hardcoded in `pgx_risk_radar.html`.
There are no `/v2/populations` or `/v2/pathways` endpoints to fetch them from
yet.

**Population clusters are too coarse in two places.** HLA-B\*15:02 runs roughly
5–12% in southern Han populations but pools to 1.81% across northwest China, and
NAT2 slow-acetylator frequency spans roughly 50% to 80% within South Asia. Single
clusters cannot represent those ranges, and gap detection inherits the error.

**Validation does not cover sourcing discipline.** `validate_resolver.py` checks
resolver behaviour thoroughly but does not assert that every frequency row
carries a resolvable `source_id`, which is what would keep the citation guarantee
true as the KB grows. There are also no tests on the FastAPI layer, so a request
model change can break the frontend without failing the suite.

---

## Security

CORS is configured `allow_origins=["*"]` with `allow_methods` and
`allow_headers` equally open, and `/explain`, `/predict`, `/v2/explain` and
`/v2/predict` are unauthenticated POST endpoints in front of a billable LLM API.
Appropriate for local development; anyone who found a deployed instance could
spend the project's Groq quota.

The API key loads from the environment or a `.env` file. There is no
`.env.example` in the repository documenting the expected variable name.

---

## Not medical advice

`PRECEDENT` results are mechanistic inferences intended to prioritise pre-trial
screening. They are not confirmed outcomes for any specific candidate and not
clinical guidance. `DIRECT` results cite real literature through the `sources`
table; the `url` on any source is worth reading before repeating what a row
says.

---

Built by shaun, joel and noel.
