"""
ARIA Stack — AI Sidecar Service
================================
Provider-agnostic LLM integration for Jenkins pipelines.
Works with: OpenAI · Azure OpenAI · Ollama · Groq · Mistral · any OpenAI-compatible API

Endpoints:
  POST /analyze-failure          — classify CI failures, suggest fixes
  POST /check-stage-relevance    — dynamic stage injection (AI Stage Gate)
  POST /check-drift              — Jenkinsfile compliance vs golden template
  POST /predict-flaky            — ML-based flaky test prediction from history
  GET  /health                   — liveness + readiness check
  GET  /metrics                  — Prometheus scrape endpoint

Run:
  uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2

Env vars:
  LLM_API_KEY     required   your OpenAI / Groq / etc. key
  LLM_BASE_URL    optional   default: https://api.openai.com/v1
  LLM_MODEL       optional   default: gpt-4o
  LLM_TEMPERATURE optional   default: 0.1  (low = deterministic, good for classification)
  LOG_LEVEL       optional   default: INFO
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from openai import OpenAI, APIError, RateLimitError
from prometheus_client import (
    Counter, Histogram, Gauge,
    generate_latest, CONTENT_TYPE_LATEST
)
from pydantic import BaseModel, Field

# ── Logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger("aria-sidecar")

# ── LLM client ───────────────────────────────────────────────────────
# openai SDK works with any OpenAI-compatible API — just change base_url
client = OpenAI(
    api_key  = os.environ["LLM_API_KEY"],
    base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
    timeout  = 30.0,
)
LLM_MODEL       = os.getenv("LLM_MODEL",       "gpt-4o")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.1"))

log.info(f"LLM backend: {os.getenv('LLM_BASE_URL', 'OpenAI')}  model={LLM_MODEL}")

# ── Prometheus metrics ────────────────────────────────────────────────
CALLS    = Counter("ai_sidecar_calls_total",    "Total AI API calls",         ["endpoint", "status"])
LATENCY  = Histogram("ai_sidecar_latency_sec",  "LLM response latency",       ["endpoint"])
FLAKY    = Counter("flaky_tests_detected_total", "Flaky tests caught by AI",   ["service"])
RETRIES  = Counter("auto_retries_triggered_total","Auto-retries scheduled",    ["service"])
DRIFT_G  = Gauge("pipeline_compliance_score",   "Last drift compliance score", ["service"])
FAILURE_TYPES = Counter("failure_classification_total","Failure types classified",["type"])

# ── In-memory flaky test history (persisted via Redis in production) ──
_flaky_history: dict[str, list[dict]] = defaultdict(list)

# ── FastAPI app ───────────────────────────────────────────────────────
app = FastAPI(
    title="ARIA AI Sidecar",
    description="AI-powered intelligence layer for Jenkins pipelines",
    version="2.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Request middleware: log every call ────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - t0
    log.info(f"{request.method} {request.url.path}  {response.status_code}  {elapsed:.3f}s")
    return response

# ── Pydantic models ───────────────────────────────────────────────────
class FailurePayload(BaseModel):
    stack_trace:  str        = Field(default="",        description="Test/build stack trace")
    log_tail:     str        = Field(default="",        description="Last 50 lines of build log")
    service:      str        = Field(default="unknown", description="Microservice name")
    branch:       str        = Field(default="unknown", description="Git branch")
    build_number: str        = Field(default="0",       description="Jenkins build number")
    test_name:    str        = Field(default="",        description="Specific test that failed")

class StagePayload(BaseModel):
    changed_files:    list[str] = Field(...,  description="Files changed in this commit")
    available_stages: list[str] = Field(...,  description="All stages defined in the pipeline")
    service:          str       = Field(default="unknown")
    branch:           str       = Field(default="unknown")

class DriftPayload(BaseModel):
    current:       str = Field(..., description="Current Jenkinsfile content")
    golden:        str = Field(..., description="Golden template Jenkinsfile")
    service_name:  str = Field(default="unknown")

class FlakyPayload(BaseModel):
    test_name:       str        = Field(..., description="Test identifier (module::class::method)")
    service:         str        = Field(default="unknown")
    recent_results:  list[bool] = Field(default=[], description="True=pass, False=fail for last N runs")

# ── LLM helper ───────────────────────────────────────────────────────
def _call_llm(endpoint: str, prompt: str, max_tokens: int = 512) -> dict[str, Any]:
    """
    Call the LLM and parse JSON response.
    Retries once on rate limit. Raises HTTPException on permanent failure.
    """
    for attempt in range(2):
        try:
            t0 = time.perf_counter()
            response = client.chat.completions.create(
                model       = LLM_MODEL,
                max_tokens  = max_tokens,
                temperature = LLM_TEMPERATURE,
                messages    = [
                    {"role": "system", "content": "You are a senior DevOps engineer. Respond ONLY with valid JSON — no markdown, no backticks, no explanation."},
                    {"role": "user",   "content": prompt},
                ],
            )
            elapsed = time.perf_counter() - t0
            LATENCY.labels(endpoint=endpoint).observe(elapsed)
            CALLS.labels(endpoint=endpoint, status="success").inc()

            raw = response.choices[0].message.content.strip()
            # Strip accidental markdown fences
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

            return json.loads(raw)

        except RateLimitError:
            if attempt == 0:
                log.warning("Rate limited — waiting 10s and retrying")
                time.sleep(10)
                continue
            CALLS.labels(endpoint=endpoint, status="rate_limited").inc()
            raise HTTPException(status_code=429, detail="LLM rate limit exceeded")

        except (APIError, json.JSONDecodeError) as exc:
            CALLS.labels(endpoint=endpoint, status="error").inc()
            log.error(f"LLM call failed: {exc}")
            raise HTTPException(status_code=502, detail=f"LLM error: {exc}")

# ─────────────────────────────────────────────────────────────────────
# ENDPOINT 1 — FAILURE ANALYSIS
# ─────────────────────────────────────────────────────────────────────
@app.post("/analyze-failure")
async def analyze_failure(payload: FailurePayload) -> dict:
    """
    Classify a CI pipeline failure using LLM.
    Returns structured JSON the pipeline uses to decide:
      - auto-retry (FLAKY_TEST above confidence threshold)
      - page on-call (INFRA_ISSUE or CODE_BUG severity HIGH+)
      - file GitHub issue (always)
    """
    log.info(f"analyze-failure  service={payload.service}  branch={payload.branch}")

    # Retrieve flaky history for this test (improves classification accuracy)
    history = _flaky_history.get(f"{payload.service}::{payload.test_name}", [])
    flaky_rate = sum(1 for r in history[-20:] if not r["passed"]) / max(len(history[-20:]), 1)

    prompt = f"""
You are a senior DevOps engineer analysing a CI pipeline failure.

SERVICE:       {payload.service}
BRANCH:        {payload.branch}
BUILD NUMBER:  {payload.build_number}
TEST NAME:     {payload.test_name or 'multiple / unknown'}
HISTORICAL FLAKY RATE (last 20 runs): {flaky_rate:.0%}

STACK TRACE:
{payload.stack_trace[:3000] or '(none provided)'}

LAST 50 LOG LINES:
{payload.log_tail[:4000] or '(none provided)'}

FAILURE_TYPE must be exactly one of:
  FLAKY_TEST          — non-deterministic; random timing, port conflicts, network blips
  INFRA_ISSUE         — Jenkins agent, Docker daemon, registry, disk space, OOM
  CODE_BUG            — assertion failure, import error, logic error in src/
  DEPENDENCY_FAILURE  — pip/npm/maven can't resolve a package
  CONFIG_ERROR        — missing env var, wrong credential, bad YAML/JSON
  BUILD_ERROR         — compilation / Dockerfile issue

Respond with ONLY this JSON (no extra keys, no markdown):
{{
  "failure_type":     "FLAKY_TEST",
  "confidence":       0.91,
  "root_cause":       "one sentence — what specifically failed and why",
  "suggested_fix":    "one actionable sentence — what the developer should do",
  "should_auto_retry": true,
  "page_oncall":      false,
  "github_label":     "flaky-test",
  "severity":         "LOW",
  "affected_component": "payment.checkout"
}}
"""

    result = _call_llm("analyze-failure", prompt, max_tokens=400)

    # Record result in flaky history
    if payload.test_name:
        key = f"{payload.service}::{payload.test_name}"
        _flaky_history[key].append({
            "passed": False,
            "build":  payload.build_number,
            "type":   result.get("failure_type"),
        })
        if len(_flaky_history[key]) > 100:
            _flaky_history[key] = _flaky_history[key][-100:]

    # Update Prometheus
    ftype = result.get("failure_type", "UNKNOWN")
    FAILURE_TYPES.labels(type=ftype).inc()
    if ftype == "FLAKY_TEST":
        FLAKY.labels(service=payload.service).inc()
    if result.get("should_auto_retry"):
        RETRIES.labels(service=payload.service).inc()

    log.info(f"failure_type={ftype}  confidence={result.get('confidence')}  auto_retry={result.get('should_auto_retry')}")
    return result


# ─────────────────────────────────────────────────────────────────────
# ENDPOINT 2 — AI STAGE GATE
# ─────────────────────────────────────────────────────────────────────
@app.post("/check-stage-relevance")
async def check_stage_relevance(payload: StagePayload) -> dict:
    """
    Given changed files, return the minimum set of pipeline stages needed.
    This is the AI Stage Gate — the single biggest win for build time.

    Rules applied by the LLM:
      - unit-test:        always
      - sast:             only if src/ files changed
      - integration-test: only if non-docs, non-test files changed
      - container-build:  only if Dockerfile, src/, or requirements changed
      - security-scan:    only if container-build is included
      - drift-check:      always (lightweight)
      - deploy:           only on main or release/* branches
    """
    log.info(f"stage-gate  service={payload.service}  changed={len(payload.changed_files)} files")

    prompt = f"""
You are a CI/CD optimisation expert for a microservices platform.

SERVICE:    {payload.service}
BRANCH:     {payload.branch}

CHANGED FILES (this commit):
{chr(10).join(f'  - {f}' for f in payload.changed_files)}

AVAILABLE PIPELINE STAGES:
{chr(10).join(f'  - {s}' for s in payload.available_stages)}

RULES — apply exactly:
  unit-test        ALWAYS required.
  drift-check      ALWAYS required (runs in 5s, always worth it).
  sast             ONLY if files under src/ changed.
  integration-test ONLY if non-docs AND non-test files changed.
  container-build  ONLY if Dockerfile OR src/ OR requirements*.txt OR pyproject.toml changed.
  security-scan    ONLY if container-build is in required_stages.
  deploy           ONLY if branch is 'main' or starts with 'release/'.

IMPORTANT: Never include a stage not in the AVAILABLE PIPELINE STAGES list.

Respond with ONLY this JSON:
{{
  "required_stages":  ["unit-test", "drift-check"],
  "skipped_stages":   ["sast", "container-build", "security-scan", "integration-test", "deploy"],
  "skip_reasons":     {{"sast": "no src/ changes", "container-build": "no Dockerfile/src changes"}},
  "estimated_savings_pct": 42
}}
"""

    result = _call_llm("check-stage-relevance", prompt, max_tokens=350)
    log.info(f"required_stages={result.get('required_stages')}  savings={result.get('estimated_savings_pct')}%")
    return result


# ─────────────────────────────────────────────────────────────────────
# ENDPOINT 3 — DRIFT DETECTION
# ─────────────────────────────────────────────────────────────────────
@app.post("/check-drift")
async def check_drift(payload: DriftPayload) -> dict:
    """
    Score the current Jenkinsfile against the golden template.
    Returns a compliance_score (0.0–1.0) and a list of deviations.
    Scores below 0.70 cause the build to go UNSTABLE.
    Security gaps (missing cosign, hardcoded credentials) are flagged separately.
    """
    log.info(f"drift-check  service={payload.service_name}")

    prompt = f"""
You are a platform engineering lead reviewing pipeline compliance.

SERVICE: {payload.service_name}

CURRENT JENKINSFILE (submitted by the team):
{payload.current[:5000]}

GOLDEN TEMPLATE (platform standard):
{payload.golden[:5000]}

Score the current Jenkinsfile against the golden template.

Scoring criteria:
  1.0 = perfect compliance
  0.9 = minor cosmetic differences only
  0.7 = missing 1–2 non-critical stages or options
  0.5 = missing critical stages (SAST, drift-check) or security steps
  0.3 = major structural differences, missing multiple required stages
  0.0 = completely non-compliant or empty

Security gaps (score these separately, they are ALWAYS high severity):
  - Hardcoded credentials (not using credentials() helper)
  - Missing Cosign container signing
  - No post {{ failure {{ ... }} }} block
  - No timeout option

Respond with ONLY this JSON:
{{
  "compliance_score":   0.97,
  "deviations":         ["missing buildDiscarder option"],
  "missing_required":   [],
  "security_gaps":      [],
  "positive_findings":  ["has Cosign signing", "has drift-check stage", "uses credentials() helper"],
  "fix_priority":       "LOW"
}}
"""

    result = _call_llm("check-drift", prompt, max_tokens=450)
    score  = result.get("compliance_score", 0)
    DRIFT_G.labels(service=payload.service_name).set(score)

    # Push to Prometheus pushgateway asynchronously (fire-and-forget)
    pushgateway = os.getenv("PROMETHEUS_PUSHGATEWAY", "http://pushgateway:9091")
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            metric = f'pipeline_compliance_score{{service="{payload.service_name}"}} {score}\n'
            await http.post(
                f"{pushgateway}/metrics/job/aria-drift",
                content=metric,
                headers={"Content-Type": "text/plain"},
            )
    except Exception:
        pass  # pushgateway is best-effort

    log.info(f"compliance_score={score}  security_gaps={result.get('security_gaps', [])}")
    return result


# ─────────────────────────────────────────────────────────────────────
# ENDPOINT 4 — FLAKY TEST PREDICTION
# ─────────────────────────────────────────────────────────────────────
@app.post("/predict-flaky")
async def predict_flaky(payload: FlakyPayload) -> dict:
    """
    Predict whether a specific test is likely to be flaky based on recent history.
    Used by the pipeline before running the full suite to decide on retry strategy.
    """
    log.info(f"predict-flaky  test={payload.test_name}  service={payload.service}")

    history_key = f"{payload.service}::{payload.test_name}"
    stored      = _flaky_history.get(history_key, [])
    all_results = payload.recent_results + [r["passed"] for r in stored[-20:]]

    if len(all_results) < 3:
        return {
            "is_likely_flaky": False,
            "flaky_probability": 0.0,
            "recommendation": "insufficient_history",
            "suggested_retry_count": 1,
        }

    pass_count = sum(1 for r in all_results if r)
    fail_count = len(all_results) - pass_count
    fail_rate  = fail_count / len(all_results)

    prompt = f"""
You are a test reliability engineer analysing a test's historical behaviour.

TEST:     {payload.test_name}
SERVICE:  {payload.service}

RECENT RESULTS ({len(all_results)} runs):
  Pass rate: {pass_count}/{len(all_results)} = {1-fail_rate:.0%}
  Fail rate: {fail_count}/{len(all_results)} = {fail_rate:.0%}

HISTORICAL FAILURES:
{json.dumps([r for r in stored[-10:] if not r.get("passed")], indent=2)}

Based on the pattern, assess whether this is a flaky (non-deterministic) test
vs a genuinely failing test that needs a code fix.

A test is "flaky" if it fails non-deterministically — inconsistent results
with no code changes between runs.

Respond with ONLY this JSON:
{{
  "is_likely_flaky":       true,
  "flaky_probability":     0.87,
  "pattern":               "intermittent — fails ~30% of runs with no code changes",
  "recommendation":        "retry_with_isolation",
  "suggested_retry_count": 3,
  "root_cause_hypothesis": "likely timing/race condition or external dependency"
}}
"""

    result = _call_llm("predict-flaky", prompt, max_tokens=300)
    return result


# ─────────────────────────────────────────────────────────────────────
# HEALTH + METRICS
# ─────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health() -> dict:
    """Jenkins depends_on: service_healthy checks this."""
    return {
        "status":  "ok",
        "model":   LLM_MODEL,
        "backend": os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
        "version": "2.0.0",
    }

@app.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> str:
    """Prometheus scrape endpoint."""
    return PlainTextResponse(
        content=generate_latest().decode(),
        media_type=CONTENT_TYPE_LATEST,
    )

@app.get("/flaky-history/{service}/{test_name}")
async def get_flaky_history(service: str, test_name: str) -> dict:
    """Debug endpoint — view flaky test history."""
    key  = f"{service}::{test_name}"
    data = _flaky_history.get(key, [])
    return {"test": key, "runs": len(data), "history": data[-20:]}
