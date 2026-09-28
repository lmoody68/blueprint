"""BLUEPRINT — FastAPI server.

Endpoints:
  GET  /              -> the single-page UI
  GET  /api/health    -> liveness + model info
  POST /api/analyze   -> {target, active?} -> {evidence, report}
  POST /api/chat      -> {question, report, evidence} -> {answer}   (grounded Q&A)

Security posture (see SECURITY.md): SSRF guard on every fetched host (collectors.public_host_ok),
per-IP rate limits + a global daily LLM cap, request-size cap, and hardening response headers.
"""
from __future__ import annotations
import os
import json as _json
import time
import traceback
from collections import defaultdict, deque
from datetime import date
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from blueprint import collectors, synthesize, llm, __version__

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")

app = FastAPI(title="BLUEPRINT", version=__version__)

# ---- abuse controls (public endpoint driving a shared LLM key) ----
_HITS: dict[str, deque] = defaultdict(deque)
_GLOBAL = {"day": date.today().isoformat(), "count": 0}
ANALYZE_PER_MIN = int(os.getenv("BLUEPRINT_ANALYZE_PER_MIN", "15"))
CHAT_PER_MIN = int(os.getenv("BLUEPRINT_CHAT_PER_MIN", "25"))
GROQ_DAILY_CAP = int(os.getenv("BLUEPRINT_GROQ_DAILY", "600"))
MAX_BODY = int(os.getenv("BLUEPRINT_MAX_BODY", str(1_000_000)))  # 1 MB


def _client_ip(req: Request) -> str:
    return (req.headers.get("cf-connecting-ip")
            or req.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (req.client.host if req.client else "?"))


def _rate_ok(ip: str, bucket: str, limit: int, window: float = 60.0) -> bool:
    now = time.time()
    dq = _HITS[bucket + ip]
    while dq and now - dq[0] > window:
        dq.popleft()
    if len(dq) >= limit:
        return False
    dq.append(now)
    return True


def _global_ok() -> bool:
    today = date.today().isoformat()
    if _GLOBAL["day"] != today:
        _GLOBAL.update(day=today, count=0)
    if _GLOBAL["count"] >= GROQ_DAILY_CAP:
        return False
    _GLOBAL["count"] += 1
    return True


async def _body(req: Request) -> dict:
    raw = await req.body()
    if len(raw) > MAX_BODY:
        raise ValueError("request body too large")
    return _json.loads(raw or b"{}")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        # 'unsafe-eval' + blob worker are required by mermaid (the architecture diagram); all other
        # external script is still refused (only self + cdnjs), and framing/base-uri stay locked.
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdnjs.cloudflare.com; "
        "worker-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'")
    return resp


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "BLUEPRINT", "version": __version__,
            "model": llm.DEFAULT_MODEL, "key_loaded": _key_ok(),
            "usage_today": _GLOBAL["count"], "daily_cap": GROQ_DAILY_CAP}


def _key_ok() -> bool:
    try:
        return bool(llm.get_groq_key())
    except Exception:
        return False


@app.post("/api/analyze")
async def analyze(req: Request):
    ip = _client_ip(req)
    if not _rate_ok(ip, "a:", ANALYZE_PER_MIN):
        return JSONResponse({"error": "Rate limit — a few requests a minute, please."}, status_code=429)
    try:
        body = await _body(req)
    except ValueError:
        return JSONResponse({"error": "Request too large."}, status_code=413)
    target = (body.get("target") or "").strip()
    active = bool(body.get("active"))
    if not target:
        return JSONResponse({"error": "Provide a target (URL, domain, or app-store link)."}, status_code=400)
    if len(target) > 2048:
        return JSONResponse({"error": "Target too long."}, status_code=400)
    t0 = time.time()
    try:
        evidence = await collectors.collect(target, active=active)
        if evidence.get("error"):   # includes the SSRF guard rejection
            return {"evidence": evidence, "report": None, "note": evidence["error"],
                    "elapsed_ms": int((time.time() - t0) * 1000)}
        if not _global_ok():
            return {"evidence": evidence, "report": None,
                    "note": "Daily analysis cap reached — resets tomorrow. (Evidence above is still yours.)",
                    "elapsed_ms": int((time.time() - t0) * 1000)}
        report = await synthesize.synthesize(evidence)
        return {"evidence": evidence, "report": report, "elapsed_ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.post("/api/chat")
async def chat(req: Request):
    ip = _client_ip(req)
    if not _rate_ok(ip, "c:", CHAT_PER_MIN):
        return JSONResponse({"error": "Rate limit — slow down a moment."}, status_code=429)
    try:
        body = await _body(req)
    except ValueError:
        return JSONResponse({"error": "Request too large."}, status_code=413)
    q = (body.get("question") or "").strip()[:1000]
    report = body.get("report") or {}
    evidence = body.get("evidence") or {}
    if not q:
        return JSONResponse({"error": "Ask a question about the teardown."}, status_code=400)
    if not report:
        return JSONResponse({"error": "Run a teardown first, then ask about it."}, status_code=400)
    if not _global_ok():
        return JSONResponse({"error": "Daily cap reached — resets tomorrow."}, status_code=429)
    try:
        return {"answer": await synthesize.ask(q, report, evidence)}
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


if os.path.isdir(STATIC):
    app.mount("/static", StaticFiles(directory=STATIC), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8975")))
