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

from blueprint import collectors, synthesize, llm, reviews, audit, __version__

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


@app.post("/api/generate_app")
async def generate_app(req: Request):
    ip = _client_ip(req)
    if not _rate_ok(ip, "b:", int(os.getenv("BLUEPRINT_BUILD_PER_MIN", "4"))):
        return JSONResponse({"error": "Rate limit — code generation is heavy; wait a moment."}, status_code=429)
    try:
        body = await _body(req)
    except ValueError:
        return JSONResponse({"error": "Request too large."}, status_code=413)
    report = body.get("report") or {}
    evidence = body.get("evidence") or {}
    if not report:
        return JSONResponse({"error": "Run a teardown first, then build."}, status_code=400)
    keep_name = (body.get("keep_name") or "").strip()[:80] or None
    goal = (body.get("goal") or "clone").strip().lower()
    if goal not in ("clone", "improve", "fix", "redesign"):
        goal = "clone"
    if not _global_ok():
        return JSONResponse({"error": "Daily cap reached — resets tomorrow."}, status_code=429)
    try:
        return await synthesize.generate_app(report, evidence, keep_name=keep_name, goal=goal)
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.post("/api/rebuild_kit")
async def rebuild_kit_ep(req: Request):
    ip = _client_ip(req)
    if not _rate_ok(ip, "k:", int(os.getenv("BLUEPRINT_KIT_PER_MIN", "5"))):
        return JSONResponse({"error": "Rate limit — wait a moment."}, status_code=429)
    try:
        body = await _body(req)
    except ValueError:
        return JSONResponse({"error": "Request too large."}, status_code=413)
    report = body.get("report") or {}
    evidence = body.get("evidence") or {}
    if not report:
        return JSONResponse({"error": "Run a teardown first, then build the kit."}, status_code=400)
    goal = (body.get("goal") or "clone").strip().lower()
    if goal not in ("clone", "improve", "fix", "redesign"):
        goal = "clone"
    if not _global_ok():
        return JSONResponse({"error": "Daily cap reached — resets tomorrow."}, status_code=429)
    try:
        return await synthesize.rebuild_kit(report, evidence, goal=goal)
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.post("/api/reviews")
async def reviews_ep(req: Request):
    """🗣️ Voice of Customer — mine PUBLIC app-store reviews for complaints → 'beat-it-here' differentiators."""
    ip = _client_ip(req)
    if not _rate_ok(ip, "r:", int(os.getenv("BLUEPRINT_REVIEWS_PER_MIN", "6"))):
        return JSONResponse({"error": "Rate limit — a few requests a minute, please."}, status_code=429)
    try:
        body = await _body(req)
    except ValueError:
        return JSONResponse({"error": "Request too large."}, status_code=413)
    query = (body.get("query") or body.get("target") or "").strip()
    if not query:
        return JSONResponse({"error": "Provide an app name or an App Store / Play Store link."}, status_code=400)
    if len(query) > 2048:
        return JSONResponse({"error": "Query too long."}, status_code=400)
    country = ((body.get("country") or "us").strip().lower()[:2]) or "us"
    t0 = time.time()
    try:
        data = await reviews.collect_reviews(query, country=country)
        meta = {k: v for k, v in data.items() if k not in ("reviews", "low_star")}  # don't ship raw reviews back
        if not data.get("ok"):
            return {"reviews_meta": meta, "voc": None,
                    "note": data.get("note") or data.get("error") or "no public reviews found",
                    "elapsed_ms": int((time.time() - t0) * 1000)}
        if not _global_ok():
            return {"reviews_meta": meta, "voc": None, "note": "Daily cap reached — resets tomorrow.",
                    "elapsed_ms": int((time.time() - t0) * 1000)}
        voc = await synthesize.voice_of_customer(data)
        return {"reviews_meta": meta, "voc": voc, "elapsed_ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.post("/api/audit")
async def audit_ep(req: Request):
    """🩺 Runtime Health Audit — load the live app in a real headless browser and MEASURE console errors,
    failed requests, accessibility (axe-core), performance, mobile tap targets, and walk its in-app views.
    This is the dimension the static teardown can't see. Everything returned is measured fact."""
    ip = _client_ip(req)
    if not _rate_ok(ip, "h:", int(os.getenv("BLUEPRINT_AUDIT_PER_MIN", "4"))):
        return JSONResponse({"error": "Rate limit — the live audit is heavy; wait a moment."}, status_code=429)
    try:
        body = await _body(req)
    except ValueError:
        return JSONResponse({"error": "Request too large."}, status_code=413)
    target = (body.get("target") or "").strip()
    if not target:
        return JSONResponse({"error": "Provide a target URL to audit."}, status_code=400)
    if len(target) > 2048:
        return JSONResponse({"error": "Target too long."}, status_code=400)
    walk = body.get("walk", True)
    t0 = time.time()
    try:
        data = await audit.run_audit(target, walk=bool(walk))
        if data.get("error"):
            return {"audit": data, "report": None, "note": data["error"],
                    "elapsed_ms": int((time.time() - t0) * 1000)}
        # LLM roadmap only if there are findings AND we have budget
        report = None
        if data.get("findings") and _global_ok():
            report = await synthesize.audit_report(data)
        elif data.get("findings"):
            report = {"note": "Daily LLM cap reached — measured findings above are complete; roadmap skipped."}
        else:
            report = await synthesize.audit_report(data)  # the cheap 'healthy' path (no LLM)
        return {"audit": data, "report": report, "elapsed_ms": int((time.time() - t0) * 1000)}
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
