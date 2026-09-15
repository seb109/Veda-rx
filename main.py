"""
Pharmacogenomic risk-radar backend.

Deterministic rule lookup decides the risk. Groq (LLM) only rephrases
the already-decided facts into a clean explanation. If Groq fails,
times out, or the API key isn't set, we silently fall back to the
static, pre-written explanation from rules.json — the demo never
breaks on stage.

Run:
    pip install fastapi uvicorn httpx python-dotenv
    export GROQ_API_KEY=your_key_here     # free at console.groq.com
    uvicorn main:app --reload --port 8000
"""

import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import resolver
from kb_loader import KBLoadError, load_kb

load_dotenv()  # loads .env if present; no-op if missing

RULES_PATH = Path(__file__).parent / "rules.json"
GENES_PATH = Path(__file__).parent / "genes.json"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")  # set in .env or environment
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"  # swap to "llama-3.1-8b-instant" if you want it faster
GROQ_TIMEOUT_SECONDS = 6.0  # fail fast -> fallback, don't let the demo hang

with open(RULES_PATH) as f:
    RULES = json.load(f)

with open(GENES_PATH) as f:
    GENES = json.load(f)

try:
    KB = load_kb()
    KB_LOAD_ERROR = None
except KBLoadError as e:
    KB = None
    KB_LOAD_ERROR = str(e)

app = FastAPI(title="PGx Risk Radar API")

# Wide open for the hackathon demo. Tighten this if you deploy it anywhere real.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ExplainRequest(BaseModel):
    drug: str


class ExplainResponse(BaseModel):
    drug: str
    gene: str
    variant: str
    region: str
    clusters: list[str]
    stat: str
    stat_label: str
    real_case: str
    explanation: str
    source: str  # "llm" or "fallback" — handy for you to see which path fired during rehearsal


class PredictRequest(BaseModel):
    drug_name: str
    gene: str  # one of the keys in genes.json, e.g. "CYP2D6"


class PredictResponse(BaseModel):
    drug_name: str
    gene: str
    gene_label: str
    region: str
    clusters: list[str]
    risk_type: str
    stat: str
    stat_label: str
    explanation: str
    source: str


def build_prompt(rule: dict) -> str:
    return (
        "You are writing a short clinical-style explanation for a hackathon demo. "
        "Use ONLY the facts below. Do not invent statistics, studies, or mechanisms "
        "not listed here. Write 2-3 plain-English sentences, no bullet points, no headers.\n\n"
        f"Drug: {rule['name']}\n"
        f"Gene/variant: {rule['gene']} ({rule['variant']})\n"
        f"Affected population: {rule['region']}\n"
        f"Mechanism: {rule['mechanism']}\n"
        f"Key statistic: {rule['stat']} — {rule['stat_label']}\n"
        f"Documented real-world case: {rule['real_case']}\n"
    )


def build_predict_prompt(drug_name: str, gene_data: dict) -> str:
    return (
        "You are writing a short pre-trial risk-screening note for a hackathon demo. "
        "A NEW, not-yet-trialed drug candidate is being screened. It has NOT been "
        "clinically tested yet — do not claim any of these effects have been observed "
        "in the new drug itself. Instead, explain the risk it would likely inherit, "
        "based ONLY on the documented population genetics facts below for the gene "
        "pathway it shares with a known, already-studied drug. Use ONLY the facts "
        "given — do not invent statistics or studies. Write 2-3 plain-English "
        "sentences, no bullet points, no headers. Make clear this is a predicted "
        "risk signal to investigate before trials, not a confirmed outcome.\n\n"
        f"New drug candidate: {drug_name}\n"
        f"Shared metabolic pathway: {gene_data['label']}\n"
        f"Population with documented elevated risk on this pathway: {gene_data['region']}\n"
        f"Mechanism: {gene_data['mechanism']}\n"
        f"Key statistic: {gene_data['stat']} — {gene_data['stat_label']}\n"
        f"Precedent (a different, already-studied drug on the same pathway): {gene_data['real_case']}\n"
        f"Drug classes this pathway typically affects: {gene_data['relevant_drug_classes']}\n"
    )


async def get_llm_explanation(rule: dict) -> str | None:
    if not GROQ_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=GROQ_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": GROQ_MODEL,
                    "messages": [{"role": "user", "content": build_prompt(rule)}],
                    "temperature": 0.3,
                    "max_tokens": 200,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception:
        # Any failure (timeout, rate limit, bad key, network blip) -> caller falls back.
        return None


async def get_llm_prediction(drug_name: str, gene_data: dict) -> str | None:
    if not GROQ_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=GROQ_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": GROQ_MODEL,
                    "messages": [{"role": "user", "content": build_predict_prompt(drug_name, gene_data)}],
                    "temperature": 0.3,
                    "max_tokens": 220,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def static_prediction(drug_name: str, gene_data: dict) -> str:
    return (
        f"{drug_name} shares its metabolic pathway ({gene_data['label']}) with drugs "
        f"already documented to carry elevated risk in {gene_data['region']}. "
        f"{gene_data['mechanism']} Before trials begin, this population should be "
        f"included in safety screening — precedent: {gene_data['real_case']}"
    )


@app.get("/")
async def root():
    """Serve the frontend HTML app."""
    return FileResponse(Path(__file__).parent / "pgx_risk_radar.html")


@app.get("/health")
async def health():
    return {"status": "ok", "groq_configured": bool(GROQ_API_KEY)}


@app.post("/explain", response_model=ExplainResponse)
async def explain(req: ExplainRequest):
    key = req.drug.strip().lower()
    rule = RULES.get(key)
    if rule is None:
        return ExplainResponse(
            drug=req.drug,
            gene="unknown",
            variant="unknown",
            region="unknown",
            clusters=[],
            stat="",
            stat_label="",
            real_case="",
            explanation="No documented case on file for this drug.",
            source="fallback",
        )

    llm_text = await get_llm_explanation(rule)
    explanation = llm_text if llm_text else rule["static_explanation"]

    return ExplainResponse(
        drug=rule["name"],
        gene=rule["gene"],
        variant=rule["variant"],
        region=rule["region"],
        clusters=rule["clusters"],
        stat=rule["stat"],
        stat_label=rule["stat_label"],
        real_case=rule["real_case"],
        explanation=explanation,
        source="llm" if llm_text else "fallback",
    )


@app.get("/genes")
async def list_genes():
    """Lets the frontend populate the gene dropdown for the predictive simulator."""
    return {key: {"label": v["label"], "region": v["region"]} for key, v in GENES.items()}


@app.post("/predict", response_model=PredictResponse)
async def predict(req: PredictRequest):
    gene_data = GENES.get(req.gene)
    if gene_data is None:
        return PredictResponse(
            drug_name=req.drug_name,
            gene=req.gene,
            gene_label="unknown pathway",
            region="unknown",
            clusters=[],
            risk_type="",
            stat="",
            stat_label="",
            explanation="No documented population data on file for this gene pathway.",
            source="fallback",
        )

    llm_text = await get_llm_prediction(req.drug_name, gene_data)
    explanation = llm_text if llm_text else static_prediction(req.drug_name, gene_data)

    return PredictResponse(
        drug_name=req.drug_name,
        gene=req.gene,
        gene_label=gene_data["label"],
        region=gene_data["region"],
        clusters=gene_data["clusters"],
        risk_type=gene_data["risk_type"],
        stat=gene_data["stat"],
        stat_label=gene_data["stat_label"],
        explanation=explanation,
        source="llm" if llm_text else "fallback",
    )


# ---------------------------------------------------------------------------
# v2 API — backed by kavach_knowledge_base_v1 via the deterministic resolver.
# The resolver decides status/priority/evidence; the LLM here is only ever
# allowed to rephrase resolver.ResolverResult into prose. It cannot see raw
# facts to invent from, and its output is discarded on any failure in favor
# of a deterministic, template-based fallback built straight from the result.
# ---------------------------------------------------------------------------


class V2ExplainRequest(BaseModel):
    drug: str
    planned_trial_regions: list[str] = []


class V2PredictRequest(BaseModel):
    drug_name: str
    gene: str  # a gene id (e.g. "CYP2D6") or variant id (e.g. "HLA-B*57:01") from the new KB
    planned_trial_regions: list[str] = []


class V2Response(BaseModel):
    query: dict
    status: str  # "DIRECT" | "PRECEDENT" | "INSUFFICIENT_EVIDENCE"
    priority: str | None
    severity: str | None
    evidence_level: str | None
    matched_gene: str | None
    matched_variant: str | None
    relationship_id: str | None
    clinical_outcome: str | None
    effect: str | None
    population_signals: list[dict]
    representation_gaps: list[str]
    sources: list[dict]
    notes: list[str]
    explanation: str
    explanation_source: str  # "llm" or "fallback"


def build_v2_explanation_prompt(result: resolver.ResolverResult, deterministic_explanation: str) -> str:
    return (
        "You are rephrasing an already-decided pharmacogenomic screening result for a "
        "hackathon demo. A deterministic rule engine — not you — has already decided the "
        "status, priority, severity, and evidence level below. Do not change, reinterpret, "
        "or second-guess any of them. Do not invent a probability of harm, a statistic, or "
        "a study that isn't listed here. Do not upgrade a PRECEDENT result into language "
        "that implies it is proven or documented for this exact drug. Write 2-4 plain-English "
        "sentences, no bullet points, no headers.\n\n"
        f"Status (already decided, do not change): {result.status}\n"
        f"Priority (already decided, do not change): {result.priority}\n"
        f"Severity: {result.severity}\n"
        f"Evidence level: {result.evidence_level}\n"
        f"Matched gene: {result.matched_gene}\n"
        f"Matched variant: {result.matched_variant}\n"
        f"Clinical outcome on file: {result.clinical_outcome}\n"
        f"Mechanism/effect on file: {result.effect}\n"
        f"Population frequency observations on file: {json.dumps(result.population_signals)}\n"
        f"Representation gaps on file: {result.representation_gaps}\n"
        f"Notes: {result.notes}\n\n"
        "A plain-language draft of the same result, for reference only (you may improve its "
        "wording but must not add new facts beyond what's listed above):\n"
        f"{deterministic_explanation}"
    )


async def get_llm_v2_explanation(result: resolver.ResolverResult, deterministic_explanation: str) -> str | None:
    if not GROQ_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=GROQ_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": GROQ_MODEL,
                    "messages": [
                        {"role": "user", "content": build_v2_explanation_prompt(result, deterministic_explanation)}
                    ],
                    "temperature": 0.3,
                    "max_tokens": 220,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def _require_kb() -> None:
    if KB is None:
        raise HTTPException(
            status_code=503,
            detail=f"kavach_knowledge_base_v1 failed to load: {KB_LOAD_ERROR}",
        )


async def _build_v2_response(query_result: resolver.ResolverResult) -> V2Response:
    deterministic = resolver.build_deterministic_explanation(query_result)
    llm_text = await get_llm_v2_explanation(query_result, deterministic)
    explanation = llm_text if llm_text else deterministic
    return V2Response(
        **query_result.to_dict(),
        explanation=explanation,
        explanation_source="llm" if llm_text else "fallback",
    )


@app.post("/v2/explain", response_model=V2Response)
async def explain_v2(req: V2ExplainRequest):
    _require_kb()
    result = resolver.resolve_explain(KB, req.drug, planned_trial_regions=req.planned_trial_regions)
    return await _build_v2_response(result)


@app.post("/v2/predict", response_model=V2Response)
async def predict_v2(req: V2PredictRequest):
    _require_kb()
    result = resolver.resolve_predict(KB, req.drug_name, req.gene, planned_trial_regions=req.planned_trial_regions)
    return await _build_v2_response(result)