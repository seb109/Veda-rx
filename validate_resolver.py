"""
Deterministic validation for kb_loader + resolver. No LLM, no network.

Run:
    python validate_resolver.py
"""

from __future__ import annotations

import json
import re
import sys

from kb_loader import load_kb
from resolver import (
    STATUS_DIRECT,
    STATUS_INSUFFICIENT_EVIDENCE,
    STATUS_PRECEDENT,
    compute_priority,
    resolve_explain,
    resolve_predict,
)

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def main() -> int:
    kb = load_kb()
    print(f"Loaded KB from {kb.root} ({len(kb.relationships)} relationships, {len(kb.warnings)} warnings)\n")

    # --- 1. Validation cases from validation_cases.json ---
    expected_pathway = {
        "codeine": "CYP2D6",
        "carbamazepine": "HLA-B*15:02",
        "clopidogrel": "CYP2C19",
        "abacavir": "HLA-B*57:01",
    }
    for case in kb.validation_cases:
        drug = case["drug"]
        result = resolve_explain(kb, drug)
        check(
            f"validation_case {case['id']} ({drug}): status DIRECT",
            result.status == STATUS_DIRECT,
            f"got {result.status}",
        )
        check(
            f"validation_case {case['id']} ({drug}): relationship_id matches expected",
            result.relationship_id == case["expected_relationship"],
            f"expected {case['expected_relationship']}, got {result.relationship_id}",
        )
        expected = expected_pathway.get(drug)
        matched = result.matched_variant or result.matched_gene
        check(
            f"validation_case {case['id']} ({drug}): matched pathway is {expected}",
            matched == expected,
            f"got {matched}",
        )

    print()

    # --- 2. Unknown gene -> INSUFFICIENT_EVIDENCE ---
    r = resolve_predict(kb, "Compound-Unknown-1", "NOT_A_REAL_GENE")
    check(
        "unknown gene/pathway -> INSUFFICIENT_EVIDENCE",
        r.status == STATUS_INSUFFICIENT_EVIDENCE,
        f"got {r.status}",
    )

    # --- 3. Unknown drug + known mechanism -> PRECEDENT, not DIRECT ---
    r = resolve_predict(kb, "Compound-New-104", "CYP2D6")
    check(
        "unknown drug on known pathway -> PRECEDENT",
        r.status == STATUS_PRECEDENT,
        f"got {r.status}",
    )
    check(
        "PRECEDENT result cites a real relationship_id as its precedent, not a fabricated one",
        r.relationship_id in kb.relationships,
        f"got {r.relationship_id}",
    )

    # --- also confirm the reverse: a drug that DOES have its own relationship
    # on that pathway resolves DIRECT, not PRECEDENT ---
    r = resolve_predict(kb, "Codeine", "CYP2D6")
    check(
        "known drug on its own documented pathway -> DIRECT (not downgraded to precedent)",
        r.status == STATUS_DIRECT,
        f"got {r.status}",
    )

    # --- 4. Weak/exploratory evidence cannot become CRITICAL ---
    check(
        "evidence ceiling: critical severity + weak evidence caps at MODERATE",
        compute_priority("critical", "weak") == "MODERATE",
        f"got {compute_priority('critical', 'weak')}",
    )
    check(
        "evidence ceiling: critical severity + exploratory evidence caps at MODERATE",
        compute_priority("critical", "exploratory") == "MODERATE",
        f"got {compute_priority('critical', 'exploratory')}",
    )
    check(
        "evidence ceiling: critical severity + strong evidence is NOT capped",
        compute_priority("critical", "strong") == "CRITICAL",
        f"got {compute_priority('critical', 'strong')}",
    )
    check(
        "evidence ceiling: missing/unknown evidence level defaults conservative (not CRITICAL)",
        compute_priority("critical", "") != "CRITICAL",
        f"got {compute_priority('critical', '')}",
    )

    # --- also: PRECEDENT itself is capped even when the underlying relationship is CRITICAL ---
    r = resolve_predict(kb, "Compound-New-HLA", "HLA-B*57:01")
    check(
        "PRECEDENT priority capped at MODERATE even when underlying relationship is CRITICAL",
        r.priority in ("MODERATE", "LOW"),
        f"got {r.priority} (underlying severity {r.severity})",
    )

    # --- 5. Population frequency is never treated as probability of harm ---
    r = resolve_explain(kb, "carbamazepine")
    dumped = json.dumps(r.to_dict())
    check(
        "resolver output contains no 'probability' field for population signals",
        "probability" not in dumped.lower(),
        "found the word 'probability' in resolver output",
    )
    for sig in r.population_signals:
        check(
            f"population signal for {sig['population']} only exposes frequency fields, not a probability",
            set(sig.keys())
            == {"variant", "population", "frequency_type", "frequency_min", "frequency_max", "unit", "notes", "sources"},
            f"got keys {sorted(sig.keys())}",
        )

    # --- 6. CYP2D6/CYP2C19 gene-level allele-frequency plumbing (new) ---
    codeine_result = resolve_explain(kb, "codeine")
    check(
        "Codeine/CYP2D6 now produces population signals",
        len(codeine_result.population_signals) > 0,
        f"got {len(codeine_result.population_signals)} signals",
    )
    check(
        "Codeine's population signals are all explicitly tagged allele_frequency",
        all(s["frequency_type"] == "allele_frequency" for s in codeine_result.population_signals),
        f"got frequency_types {[s['frequency_type'] for s in codeine_result.population_signals]}",
    )
    check(
        "Codeine's population signals are all CYP2D6 alleles (no cross-gene leakage)",
        all((s["variant"] or "").startswith("CYP2D6") for s in codeine_result.population_signals),
        f"got variants {[s['variant'] for s in codeine_result.population_signals]}",
    )

    clopidogrel_result = resolve_explain(kb, "clopidogrel")
    check(
        "Clopidogrel/CYP2C19 now produces population signals",
        len(clopidogrel_result.population_signals) > 0,
        f"got {len(clopidogrel_result.population_signals)} signals",
    )
    check(
        "Clopidogrel's population signals are all explicitly tagged allele_frequency",
        all(s["frequency_type"] == "allele_frequency" for s in clopidogrel_result.population_signals),
        f"got frequency_types {[s['frequency_type'] for s in clopidogrel_result.population_signals]}",
    )
    check(
        "Clopidogrel's population signals are all CYP2C19 alleles (no cross-gene leakage)",
        all((s["variant"] or "").startswith("CYP2C19") for s in clopidogrel_result.population_signals),
        f"got variants {[s['variant'] for s in clopidogrel_result.population_signals]}",
    )

    for label, result in (("codeine", codeine_result), ("clopidogrel", clopidogrel_result)):
        dumped = json.dumps(result.to_dict())
        check(
            f"{label} resolver output has no field/key literally named 'probability'",
            not any("probability" in k.lower() for sig in result.population_signals for k in sig.keys()),
            "found a 'probability'-named key in a population signal",
        )
        # The word "probability" itself is allowed to appear ONLY inside an
        # explicit disclaimer ("not a probability ..."); it must never be
        # asserted as a fact about the allele frequency.
        mentions = re.findall(r".{0,12}probability", dumped, re.IGNORECASE)
        check(
            f"{label}: every mention of 'probability' in the output is a disclaimer, never an assertion",
            all("not a" in m.lower() for m in mentions),
            f"non-disclaiming 'probability' mention(s): {mentions}",
        )
        check(
            f"{label}'s allele-frequency signals are never labeled as carrier/phenotype/adverse-event frequency",
            not any(
                bad in dumped.lower()
                for bad in ("carrier_frequency", "phenotype_frequency", "adverse_event_probability")
            ),
            f"found a disallowed frequency label in {label}'s resolver output",
        )

    # --- HLA-B*15:02 / HLA-B*57:01 behavior is byte-for-byte unchanged by the
    # new gene-level lookup path (these relationships carry a `variant`, not
    # a `gene`, so they must take the pre-existing variant-only branch) ---
    carbamazepine_result = resolve_explain(kb, "carbamazepine")
    check(
        "HLA-B*15:02 (carbamazepine) still returns exactly 4 population signals",
        len(carbamazepine_result.population_signals) == 4,
        f"got {len(carbamazepine_result.population_signals)}",
    )
    check(
        "HLA-B*15:02 signals are still frequency_type 'reported_range' (untouched by allele_frequency work)",
        all(s["frequency_type"] == "reported_range" for s in carbamazepine_result.population_signals),
        f"got {[s['frequency_type'] for s in carbamazepine_result.population_signals]}",
    )

    abacavir_result = resolve_explain(kb, "abacavir")
    check(
        "HLA-B*57:01 (abacavir) still returns exactly 2 population signals",
        len(abacavir_result.population_signals) == 2,
        f"got {len(abacavir_result.population_signals)}",
    )
    check(
        "HLA-B*57:01 signals are still their original frequency_types (untouched by allele_frequency work)",
        {s["frequency_type"] for s in abacavir_result.population_signals} == {"reported_estimate", "reported_maximum"},
        f"got {[s['frequency_type'] for s in abacavir_result.population_signals]}",
    )

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
