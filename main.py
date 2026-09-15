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
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

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