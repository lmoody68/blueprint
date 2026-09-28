# BLUEPRINT

**Reverse-engineer any app from public signals into a build-and-beat playbook.**

Point BLUEPRINT at a product (URL, domain, or app-store link) and it infers *how it was likely
built* from **observable public evidence**, then produces: the inferred stack, an architecture
diagram, a step-by-step build playbook, ranked improvements, and an MVP clone spec — every claim
tagged with its evidence and a confidence level.

## Why it's legitimate (the red lines)

BLUEPRINT is a **product/architecture teardown** tool — the same thing engineers, product teams,
and investors do by hand. It works only from signals a browser already sees. Enforced in code:

- ✅ Honors `robots.txt`; rate-limited; normal browser User-Agent; bounded fetches.
- ✅ "Deep recon" (GraphQL introspection / endpoint probing) is **opt-in** only.
- ❌ **No dark web.** No stolen source, leaked data, or cracked binaries.
- ❌ **No decompiling binaries / bypassing DRM** (DMCA §1201).
- ❌ **No copying proprietary code or assets** — it *infers* architecture, it never lifts IP.
- ❌ **No auth-scraping** behind logins you aren't entitled to.

The synthesis prompt forces the model to ground every technology claim in the supplied evidence,
distinguish **observed** from **inferred**, and refuse to fabricate internals it can't see.

## Run it

```bat
Start_BLUEPRINT.bat        ::  or:  python app.py
```
Then open **http://127.0.0.1:8975**, enter a site (e.g. `linear.app`), and click Analyze.

Requirements: `pip install -r requirements.txt` (FastAPI, uvicorn, httpx, beautifulsoup4).
Groq key is read at runtime from `Documents/API_Keys.txt` (first `gsk_` token) — never hardcoded.

## Architecture

```
intake ─▶ collectors ─▶ synthesize (Groq LLM) ─▶ report UI
          │
          ├─ web_fingerprint   headers / cookies / assets → signature DB (signatures.py)
          ├─ dns_hosting       DNS, IP/ASN, TLS cert
          ├─ endpoints         API/GraphQL paths in delivered markup  (+ active probe, opt-in)
          ├─ github            public org + repos (languages!)
          └─ market_signals    pricing tiers → data-model hints
```

- `app.py` — FastAPI server (`/`, `/api/health`, `/api/analyze`)
- `blueprint/collectors.py` — signal gathering (red lines enforced here)
- `blueprint/signatures.py` — Wappalyzer-style tech fingerprint DB
- `blueprint/synthesize.py` — evidence → grounded teardown JSON
- `blueprint/llm.py` — Groq client (curl UA to dodge Cloudflare 1010)
- `static/index.html` — single-page report UI (tabs + mermaid diagram)

## Roadmap

- **Phase 1 (done):** web passive fingerprint + DNS/TLS + GitHub/pricing signals + LLM synthesis + UI.
- **Phase 2:** deep-active recon (live endpoint/GraphQL mapping, JS-bundle crawl), **mobile** teardown
  (store metadata, permissions, SDK/tracker fingerprinting, review pain-mining), job-listing stack mining.
- **Phase 3:** deploy to `blueprint.mac-vision.com` (Proxmox LXC + CF tunnel), report exports (MD/JSON/PDF).
