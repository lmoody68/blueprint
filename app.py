"""BLUEPRINT — FastAPI server.

Endpoints:
  GET  /              -> the single-page UI
  GET  /api/health    -> liveness + model info
  POST /api/analyze   -> {target, active?} -> {evidence, report}
"""
from __future__ import annotations
import os
import time
import traceback
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from blueprint import collectors, synthesize, llm, __version__

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")

app = FastAPI(title="BLUEPRINT", version=__version__)


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "BLUEPRINT", "version": __version__,
            "model": llm.DEFAULT_MODEL, "key_loaded": _key_ok()}


def _key_ok() -> bool:
    try:
        return bool(llm.get_groq_key())
    except Exception:
        return False


@app.post("/api/analyze")
async def analyze(req: Request):
    body = await req.json()
    target = (body.get("target") or "").strip()
    active = bool(body.get("active"))
    if not target:
        return JSONResponse({"error": "Provide a target (URL, domain, or app-store link)."}, status_code=400)
    t0 = time.time()
    try:
        evidence = await collectors.collect(target, active=active)
        if evidence.get("kind") == "mobile" or evidence.get("error"):
            return {"evidence": evidence, "report": None,
                    "note": evidence.get("error") or "Mobile teardown is Phase 2 — metadata only for now.",
                    "elapsed_ms": int((time.time() - t0) * 1000)}
        report = await synthesize.synthesize(evidence)
        return {"evidence": evidence, "report": report, "elapsed_ms": int((time.time() - t0) * 1000)}
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
