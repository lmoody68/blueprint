# BLUEPRINT — Security Plan & Posture

BLUEPRINT is a public web service that **fetches user-supplied URLs server-side** and **drives an LLM
on a shared key**. That threat model has three sharp edges — SSRF, abuse/cost, and injection — plus the
usual web hardening. Here is the plan, what's enforced, and what's accepted as residual risk.

## Threat model → controls

| # | Threat | Control (enforced in code) | Where |
|---|--------|----------------------------|-------|
| 1 | **SSRF** — caller aims BLUEPRINT at internal IPs, `localhost`, or cloud metadata (`169.254.169.254`) to probe/exfiltrate the private network | `public_host_ok()` resolves the target and **rejects** loopback / private / link-local / reserved / multicast / unspecified (IPv4+IPv6) before any fetch; named hosts `localhost`/`metadata.*` blocked outright | `blueprint/collectors.py` |
| 2 | **Cost / abuse** — someone hammers `/api/analyze` or `/api/chat` to burn the shared Groq key | Per-IP sliding-window rate limits (analyze 15/min, chat 25/min) keyed on the real client IP (`CF-Connecting-IP`), **plus a global daily LLM cap** (600/day) that fails closed | `app.py` |
| 3 | **Prompt injection** — a malicious target page embeds "ignore your instructions" | BLUEPRINT sends the LLM **structured evidence** (headers, detections, endpoints, trimmed pricing excerpt) — *not* raw page HTML — and the output is a **fixed JSON schema**. Injection can at worst skew a field, never exfiltrate or change server behavior. The evidence payload is length-capped. | `blueprint/synthesize.py` |
| 4 | **Resource exhaustion** — huge pages, redirect loops, giant request bodies | Bounded fetch timeouts, capped connections, response slices; **1 MB request-body cap**; nginx `client_max_body_size 2m`; active bundle crawl limited to 4 files | `app.py`, `collectors.py`, nginx |
| 5 | **Clickjacking / MIME / referrer leakage** | Response headers: `X-Frame-Options: DENY` + CSP `frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy` locking down camera/mic/geo, and a **Content-Security-Policy** (self + inline + cdnjs for mermaid only) | `app.py` middleware |
| 6 | **Secret exposure** — the Groq key | Key lives only in `/opt/blueprint/blueprint.env` (`chmod 600`, systemd `EnvironmentFile`); never in the repo (`.gitignore`), never logged, never returned by any endpoint | container |
| 7 | **Transport** | Public traffic is HTTPS-only via the Cloudflare tunnel (TLS terminated at CF edge); origin is not directly internet-exposed | Cloudflare + Proxmox |

## Ethical/legal guardrails (product-level, also "security")
Enforced in `collectors.py`: honors `robots.txt`, deep-recon is **opt-in**, and hard red lines — **no dark
web, no binary decompilation, no DRM bypass, no auth-scraping, no lifting proprietary code**. BLUEPRINT
infers from public signals only.

## Residual risks (accepted, documented)
- **Redirect-based SSRF:** the guard checks the *initial* host; a target that 3xx-redirects to an internal
  address after passing the check is a residual edge. Mitigation: redirects are capped and the origin has no
  privileged internal services worth reaching. *Future:* re-validate each redirect hop.
- **In-memory rate limiter** resets on service restart and isn't shared across replicas (single instance today).
- **Shared LLM key** — the global daily cap bounds worst-case spend; a per-key budget alert is a future add.

## Tuning knobs (env)
`BLUEPRINT_ANALYZE_PER_MIN`, `BLUEPRINT_CHAT_PER_MIN`, `BLUEPRINT_GROQ_DAILY`, `BLUEPRINT_MAX_BODY`.

## Verification
- SSRF: `POST /api/analyze {"target":"127.0.0.1"}` / `localhost` / `169.254.169.254` → blocked with a guard note.
- Headers: every response carries the six hardening headers above.
- Rate limit: >15 analyses/min from one IP → `429`.
