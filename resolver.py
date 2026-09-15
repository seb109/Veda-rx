"""
Deterministic resolver over the kavach_knowledge_base_v1 KnowledgeBase.

No LLM calls happen anywhere in this module. Every field in the returned
result is either copied verbatim from the KB or computed by a fixed,
auditable rule. Population frequency is never converted into a
probability of harm, and a mechanistic precedent is never returned with
the same status as a documented (DIRECT) relationship.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kb_loader import KnowledgeBase

STATUS_DIRECT = "DIRECT"
STATUS_PRECEDENT = "PRECEDENT"
STATUS_INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"

PRIORITY_ORDER = ["LOW", "MODERATE", "HIGH", "CRITICAL"]

SEVERITY_TO_PRIORITY = {
    "critical": "CRITICAL",
    "high": "HIGH",
    "moderate": "MODERATE",
    "low": "LOW",
}

# RULE-005 (evidence ceiling): anything short of "strong" evidence cannot
# report a priority above MODERATE, no matter how severe the underlying
# clinical outcome is documented to be. An unrecognized/missing evidence
# level is treated as weak (conservative default), not as strong.
EVIDENCE_CEILING = {
    "strong": None,
    "moderate": "MODERATE",
    "weak": "MODERATE",
    "exploratory": "MODERATE",
}

# A mechanistic precedent is inherently unproven for the candidate drug
# (README guardrail: "not proof that the hypothetical drug causes the
# same clinical outcome"). Its priority is therefore capped at MODERATE
# even if the precedent relationship itself is CRITICAL/strong.
PRECEDENT_PRIORITY_CEILING = "MODERATE"


def _cap(priority: str, ceiling: str | None) -> str:
    if ceiling is None:
        return priority
    if PRIORITY_ORDER.index(priority) > PRIORITY_ORDER.index(ceiling):
        return ceiling
    return priority


def compute_priority(severity: str | None, evidence_level: str | None) -> str:
    base = SEVERITY_TO_PRIORITY.get((severity or "").lower(), "LOW")
    ceiling = EVIDENCE_CEILING.get((evidence_level or "").lower(), "MODERATE")
    return _cap(base, ceiling)


@dataclass
class ResolverResult:
    query: dict[str, Any]
    status: str
    priority: str | None = None
    severity: str | None = None
    evidence_level: str | None = None
    matched_gene: str | None = None
    matched_variant: str | None = None
    relationship_id: str | None = None
    clinical_outcome: str | None = None
    effect: str | None = None
    population_signals: list[dict] = field(default_factory=list)
    representation_gaps: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "status": self.status,
            "priority": self.priority,
            "severity": self.severity,
            "evidence_level": self.evidence_level,
            "matched_gene": self.matched_gene,
            "matched_variant": self.matched_variant,
            "relationship_id": self.relationship_id,
            "clinical_outcome": self.clinical_outcome,
            "effect": self.effect,
            "population_signals": self.population_signals,
            "representation_gaps": self.representation_gaps,
            "sources": self.sources,
            "notes": self.notes,
        }


def _resolve_sources(kb: KnowledgeBase, source_ids: list[str]) -> list[dict]:
    resolved = []
    for sid in source_ids:
        src = kb.sources.get(sid)
        if src is not None:
            resolved.append(
                {
                    "id": src["id"],
                    "organization": src.get("organization"),
                    "title": src.get("title"),
                    "url": src.get("url"),
                }
            )
    return resolved


def _freq_row_to_signal(kb: KnowledgeBase, freq: dict) -> dict:
    return {
        "variant": freq.get("variant"),
        "population": freq.get("population"),
        "frequency_type": freq.get("frequency_type"),
        "frequency_min": freq.get("frequency_min"),
        "frequency_max": freq.get("frequency_max"),
        "unit": freq.get("unit"),
        "notes": freq.get("notes"),
        "sources": _resolve_sources(kb, freq.get("source_ids", [])),
    }


def _population_signals_for(
    kb: KnowledgeBase,
    variant_id: str | None = None,
    gene_id: str | None = None,
) -> list[dict]:
    """Presentation-evidence lookup only — no phenotype inference, no scoring.

    HLA-style relationships carry an explicit `variant`, so they resolve here
    exactly as before. CYP2D6/CYP2C19-style relationships carry a `gene`
    instead (their phenotype is diplotype-based, not a single-allele-carrier
    fact), so for those we additionally pull allele-frequency rows for every
    variant.json entry that belongs to that gene. This surfaces documented
    per-allele frequency evidence for the gene pathway; it does not attempt
    to combine those allele frequencies into a metabolizer-phenotype
    frequency, and it does not filter by which specific phenotype (e.g.
    ultrarapid vs. poor) a given allele happens to support — each returned
    signal is self-labeled with its own `variant` id and frequency_type so a
    caller can distinguish them.
    """
    signals = []
    seen_variants = set()

    if variant_id:
        for freq in kb.frequencies_by_variant.get(variant_id, []):
            signals.append(_freq_row_to_signal(kb, freq))
        seen_variants.add(variant_id)

    if gene_id:
        gene_variant_ids = sorted(
            v_id for v_id, v in kb.variants.items()
            if v.get("gene") == gene_id and v_id not in seen_variants
        )
        for v_id in gene_variant_ids:
            for freq in kb.frequencies_by_variant.get(v_id, []):
                signals.append(_freq_row_to_signal(kb, freq))

    return signals


def _representation_gaps(population_signals: list[dict], planned_trial_regions: list[str] | None) -> list[str]:
    if not planned_trial_regions:
        return []
    planned = {r.upper() for r in planned_trial_regions}
    gaps = []
    for sig in population_signals:
        pop = sig.get("population")
        freq_max = sig.get("frequency_max") or 0
        if pop and pop.upper() not in planned and freq_max > 0:
            gaps.append(pop)
    return gaps


def _best_relationship(relationships: list[dict]) -> dict:
    def rank(rel: dict) -> int:
        return PRIORITY_ORDER.index(compute_priority(rel.get("severity"), rel.get("evidence_level")))

    best = relationships[0]
    best_rank = rank(best)
    for rel in relationships[1:]:
        r = rank(rel)
        if r > best_rank:
            best, best_rank = rel, r
    return best


def resolve_explain(
    kb: KnowledgeBase,
    drug_name: str,
    planned_trial_regions: list[str] | None = None,
) -> ResolverResult:
    query = {"drug": drug_name}
    drug_id = drug_name.strip().lower()

    relationships = kb.relationships_by_drug.get(drug_id)
    if not relationships:
        notes = []
        if drug_id not in kb.drugs:
            notes.append(f"'{drug_name}' is not present in the knowledge base's drug list.")
        else:
            notes.append(
                f"'{drug_name}' is known to the knowledge base but has no documented "
                f"gene/variant relationship yet (status: {kb.drugs[drug_id].get('status')})."
            )
        return ResolverResult(query=query, status=STATUS_INSUFFICIENT_EVIDENCE, notes=notes)

    rel = _best_relationship(relationships)
    priority = compute_priority(rel.get("severity"), rel.get("evidence_level"))
    variant_id = rel.get("variant")
    population_signals = _population_signals_for(kb, variant_id, rel.get("gene"))

    return ResolverResult(
        query=query,
        status=STATUS_DIRECT,
        priority=priority,
        severity=rel.get("severity"),
        evidence_level=rel.get("evidence_level"),
        matched_gene=rel.get("gene"),
        matched_variant=rel.get("variant"),
        relationship_id=rel.get("id"),
        clinical_outcome=rel.get("clinical_outcome"),
        effect=rel.get("effect"),
        population_signals=population_signals,
        representation_gaps=_representation_gaps(population_signals, planned_trial_regions),
        sources=_resolve_sources(kb, rel.get("source_ids", [])),
    )


def resolve_predict(
    kb: KnowledgeBase,
    drug_name: str,
    pathway_id: str,
    planned_trial_regions: list[str] | None = None,
) -> ResolverResult:
    query = {"drug_name": drug_name, "pathway": pathway_id}
    drug_id = drug_name.strip().lower()

    is_gene = pathway_id in kb.genes
    is_variant = pathway_id in kb.variants
    if not is_gene and not is_variant:
        return ResolverResult(
            query=query,
            status=STATUS_INSUFFICIENT_EVIDENCE,
            notes=[f"'{pathway_id}' is not a known gene or variant in the knowledge base."],
        )

    candidates = kb.relationships_by_gene.get(pathway_id, []) if is_gene else kb.relationships_by_variant.get(pathway_id, [])
    if not candidates:
        return ResolverResult(
            query=query,
            status=STATUS_INSUFFICIENT_EVIDENCE,
            matched_gene=pathway_id if is_gene else kb.variants[pathway_id].get("gene"),
            matched_variant=pathway_id if is_variant else None,
            notes=[f"'{pathway_id}' is a recognized pathway but has no documented relationships yet."],
        )

    direct_matches = [rel for rel in candidates if rel.get("drug") == drug_id]
    if direct_matches:
        rel = _best_relationship(direct_matches)
        priority = compute_priority(rel.get("severity"), rel.get("evidence_level"))
        population_signals = _population_signals_for(kb, rel.get("variant"), rel.get("gene"))
        return ResolverResult(
            query=query,
            status=STATUS_DIRECT,
            priority=priority,
            severity=rel.get("severity"),
            evidence_level=rel.get("evidence_level"),
            matched_gene=rel.get("gene"),
            matched_variant=rel.get("variant"),
            relationship_id=rel.get("id"),
            clinical_outcome=rel.get("clinical_outcome"),
            effect=rel.get("effect"),
            population_signals=population_signals,
            representation_gaps=_representation_gaps(population_signals, planned_trial_regions),
            sources=_resolve_sources(kb, rel.get("source_ids", [])),
        )

    # RULE-002: candidate drug is not documented on this pathway, but the
    # pathway itself has documented precedent from other drugs sharing the
    # same mechanism. This is a screening signal, not a proven outcome.
    rel = _best_relationship(candidates)
    base_priority = compute_priority(rel.get("severity"), rel.get("evidence_level"))
    priority = _cap(base_priority, PRECEDENT_PRIORITY_CEILING)
    population_signals = _population_signals_for(kb, rel.get("variant"), rel.get("gene"))

    return ResolverResult(
        query=query,
        status=STATUS_PRECEDENT,
        priority=priority,
        severity=rel.get("severity"),
        evidence_level=rel.get("evidence_level"),
        matched_gene=rel.get("gene"),
        matched_variant=rel.get("variant"),
        relationship_id=rel.get("id"),
        clinical_outcome=rel.get("clinical_outcome"),
        effect=rel.get("effect"),
        population_signals=population_signals,
        representation_gaps=_representation_gaps(population_signals, planned_trial_regions),
        sources=_resolve_sources(kb, rel.get("source_ids", [])),
        notes=[
            f"'{drug_name}' has no documented relationship of its own. This result is a "
            f"mechanistic precedent borrowed from drug '{rel.get('drug')}' on the same "
            f"pathway, not a confirmed outcome for '{drug_name}'."
        ],
    )


def build_deterministic_explanation(result: ResolverResult) -> str:
    if result.status == STATUS_DIRECT:
        parts = [
            f"Documented relationship: {result.matched_variant or result.matched_gene} is "
            f"associated with {result.clinical_outcome or 'a documented clinical outcome'}"
            + (f" via {result.effect}." if result.effect else "."),
            f"Severity: {result.severity or 'unknown'}. Evidence level: {result.evidence_level or 'unknown'}.",
        ]
        if result.population_signals:
            freq_bits = [
                f"{s['population']} {s.get('frequency_min')}-{s.get('frequency_max')} {s.get('unit', '')}".strip()
                for s in result.population_signals
            ]
            parts.append("Documented population frequency observations: " + "; ".join(freq_bits) + ".")
        if result.representation_gaps:
            parts.append(
                "Populations with a documented signal not covered by the planned trial regions: "
                + ", ".join(result.representation_gaps) + "."
            )
        if result.sources:
            parts.append("Sources: " + "; ".join(s["title"] for s in result.sources if s.get("title")) + ".")
        return " ".join(parts)

    if result.status == STATUS_PRECEDENT:
        parts = [
            f"No documented relationship exists for this drug on {result.matched_variant or result.matched_gene}.",
            f"This pathway does have a documented precedent from another drug ({result.relationship_id}), "
            f"producing {result.clinical_outcome or 'a related clinical outcome'} in that documented case.",
            "This is a mechanistic precedent only, not a confirmed outcome for the candidate drug — "
            "treat it as a pre-trial screening signal to investigate, not a diagnosis.",
        ]
        if result.population_signals:
            freq_bits = [
                f"{s['population']} {s.get('frequency_min')}-{s.get('frequency_max')} {s.get('unit', '')}".strip()
                for s in result.population_signals
            ]
            parts.append("Documented population frequency observations on this pathway: " + "; ".join(freq_bits) + ".")
        return " ".join(parts)

    reason = result.notes[0] if result.notes else "No supported relationship was found for this query."
    return f"Insufficient evidence to generate a risk signal. {reason}"
