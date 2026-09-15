"""
Loader for the kavach_knowledge_base_v1 relational KB.

Read-only: never writes to the KB directory. Fails loudly and specifically
on missing/malformed files rather than silently returning partial data,
so a broken KB can't quietly degrade the resolver into fabricating facts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REQUIRED_FILES = [
    "genes.json",
    "variants.json",
    "phenotypes.json",
    "mechanisms.json",
    "populations.json",
    "drugs.json",
    "relationships.json",
    "population_frequencies.json",
    "sources.json",
    "rules.json",
    "validation_cases.json",
]

DEFAULT_KB_DIRNAME = "kavach_knowledge_base_v1"


class KBLoadError(Exception):
    pass


@dataclass
class KnowledgeBase:
    genes: dict[str, dict] = field(default_factory=dict)
    variants: dict[str, dict] = field(default_factory=dict)
    phenotypes: dict[str, dict] = field(default_factory=dict)
    mechanisms: dict[str, dict] = field(default_factory=dict)
    populations: dict[str, dict] = field(default_factory=dict)
    drugs: dict[str, dict] = field(default_factory=dict)
    relationships: dict[str, dict] = field(default_factory=dict)
    sources: dict[str, dict] = field(default_factory=dict)
    rules: dict[str, dict] = field(default_factory=dict)
    validation_cases: list[dict] = field(default_factory=list)
    population_frequencies: list[dict] = field(default_factory=list)

    relationships_by_drug: dict[str, list[dict]] = field(default_factory=dict)
    relationships_by_gene: dict[str, list[dict]] = field(default_factory=dict)
    relationships_by_variant: dict[str, list[dict]] = field(default_factory=dict)
    frequencies_by_variant: dict[str, list[dict]] = field(default_factory=dict)

    warnings: list[str] = field(default_factory=list)
    root: Path | None = None


def _find_kb_root(base: Path, max_depth: int = 4) -> Path:
    frontier = [base]
    for _ in range(max_depth):
        next_frontier: list[Path] = []
        for candidate in frontier:
            if not candidate.is_dir():
                continue
            if all((candidate / name).is_file() for name in REQUIRED_FILES):
                return candidate
            next_frontier.extend(p for p in sorted(candidate.iterdir()) if p.is_dir())
        frontier = next_frontier
    raise KBLoadError(
        f"Could not find a directory under '{base}' containing all required KB "
        f"files. Looked for: {REQUIRED_FILES}"
    )


def _load_json_list(path: Path) -> list[dict]:
    data = _load_json(path)
    if not isinstance(data, list):
        raise KBLoadError(f"{path} must contain a JSON array, got {type(data).__name__}")
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise KBLoadError(f"{path}[{i}] must be a JSON object, got {type(item).__name__}")
    return data


def _load_json(path: Path) -> Any:
    if not path.is_file():
        raise KBLoadError(f"Missing required KB file: {path}")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise KBLoadError(f"Malformed JSON in {path}: {e}") from e


def _index_by_id(items: list[dict], filename: str, warnings: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i, item in enumerate(items):
        item_id = item.get("id")
        if not item_id:
            raise KBLoadError(f"{filename}[{i}] is missing required field 'id'")
        if item_id in out:
            raise KBLoadError(f"{filename} has duplicate id '{item_id}'")
        out[item_id] = item
    return out


def _group_by(items: list[dict], key: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for item in items:
        value = item.get(key)
        if not value:
            continue
        out.setdefault(value, []).append(item)
    return out


def load_kb(base_path: str | Path | None = None) -> KnowledgeBase:
    base = Path(base_path) if base_path is not None else Path(__file__).parent / DEFAULT_KB_DIRNAME
    if not base.exists():
        raise KBLoadError(f"KB base path does not exist: {base}")

    root = _find_kb_root(base)
    warnings: list[str] = []

    genes_raw = _load_json_list(root / "genes.json")
    variants_raw = _load_json_list(root / "variants.json")
    phenotypes_raw = _load_json_list(root / "phenotypes.json")
    mechanisms_raw = _load_json_list(root / "mechanisms.json")
    populations_raw = _load_json_list(root / "populations.json")
    drugs_raw = _load_json_list(root / "drugs.json")
    relationships_raw = _load_json_list(root / "relationships.json")
    sources_raw = _load_json_list(root / "sources.json")
    rules_raw = _load_json_list(root / "rules.json")
    validation_cases_raw = _load_json_list(root / "validation_cases.json")
    frequencies_raw = _load_json_list(root / "population_frequencies.json")

    genes = _index_by_id(genes_raw, "genes.json", warnings)
    variants = _index_by_id(variants_raw, "variants.json", warnings)
    phenotypes = _index_by_id(phenotypes_raw, "phenotypes.json", warnings)
    mechanisms = _index_by_id(mechanisms_raw, "mechanisms.json", warnings)
    populations = _index_by_id(populations_raw, "populations.json", warnings)
    drugs = _index_by_id(drugs_raw, "drugs.json", warnings)
    relationships = _index_by_id(relationships_raw, "relationships.json", warnings)
    sources = _index_by_id(sources_raw, "sources.json", warnings)
    rules = _index_by_id(rules_raw, "rules.json", warnings)

    for rel in relationships_raw:
        for ref_key, ref_index, ref_label in (
            ("gene", genes, "genes.json"),
            ("variant", variants, "variants.json"),
            ("phenotype", phenotypes, "phenotypes.json"),
        ):
            ref_value = rel.get(ref_key)
            if ref_value and ref_value not in ref_index:
                warnings.append(
                    f"relationships.json[{rel.get('id')}] references unknown "
                    f"{ref_key} '{ref_value}' not found in {ref_label}"
                )
        for source_id in rel.get("source_ids", []):
            if source_id not in sources:
                warnings.append(
                    f"relationships.json[{rel.get('id')}] references unknown source_id '{source_id}'"
                )

    for freq in frequencies_raw:
        if "variant" not in freq or "population" not in freq:
            raise KBLoadError(
                f"population_frequencies.json entry missing 'variant' or 'population': {freq}"
            )
        if freq["variant"] not in variants:
            warnings.append(
                f"population_frequencies.json references unknown variant '{freq['variant']}'"
            )
        if freq["population"] not in populations:
            warnings.append(
                f"population_frequencies.json references unknown population '{freq['population']}'"
            )

    kb = KnowledgeBase(
        genes=genes,
        variants=variants,
        phenotypes=phenotypes,
        mechanisms=mechanisms,
        populations=populations,
        drugs=drugs,
        relationships=relationships,
        sources=sources,
        rules=rules,
        validation_cases=validation_cases_raw,
        population_frequencies=frequencies_raw,
        relationships_by_drug=_group_by(relationships_raw, "drug"),
        relationships_by_gene=_group_by(relationships_raw, "gene"),
        relationships_by_variant=_group_by(relationships_raw, "variant"),
        frequencies_by_variant=_group_by(frequencies_raw, "variant"),
        warnings=warnings,
        root=root,
    )
    return kb


if __name__ == "__main__":
    kb = load_kb()
    print(f"Loaded KB from {kb.root}")
    print(f"  genes: {len(kb.genes)}  variants: {len(kb.variants)}  drugs: {len(kb.drugs)}")
    print(f"  relationships: {len(kb.relationships)}  sources: {len(kb.sources)}")
    print(f"  population_frequencies: {len(kb.population_frequencies)}")
    print(f"  validation_cases: {len(kb.validation_cases)}")
    if kb.warnings:
        print(f"  warnings ({len(kb.warnings)}):")
        for w in kb.warnings:
            print(f"    - {w}")
