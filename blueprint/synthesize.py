"""LLM synthesis — turn collected public signals into a grounded build-and-beat playbook.

The system prompt forces the model to (a) ground every inferred technology in the supplied
evidence, (b) label confidence, and (c) refuse to fabricate proprietary internals it cannot
observe. This mirrors BLUEPRINT's whole thesis: infer from signals, never claim what wasn't seen.
"""
from __future__ import annotations
import json
from . import llm

SYSTEM = """You are BLUEPRINT, a principal software architect who performs LEGAL product teardowns.
You infer how a product was likely built from PUBLIC, observable signals only (tech fingerprints,
headers, asset URLs, DNS/TLS, public GitHub, pricing). You NEVER claim access to private source,
databases, or proprietary internals, and you NEVER invent specifics you cannot support.

Rules:
- Ground every technology claim in the provided evidence. If evidence is thin, say so and lower confidence.
- Distinguish OBSERVED (in the signals) from INFERRED (reasonable architectural guess).
- Be concrete and useful: a builder should be able to follow your playbook to recreate a comparable app.
- Improvements must be specific and defensible, tied to the product's likely gaps.

Return ONLY a JSON object with EXACTLY these keys:
{
  "summary": "2-3 sentence plain-English read on what this product is and how it's likely built",
  "confidence_overall": "low|medium|high",
  "composite_score": 0,
  "composite_rationale": "",
  "business_model": {"type": "", "basis": "observed|inferred", "evidence": ""},
  "inferred_stack": {
    "frontend": [{"tech": "", "confidence": "low|medium|high", "basis": "observed|inferred", "evidence": ""}],
    "backend": [ ... same shape ... ],
    "database": [ ... ],
    "infra": [ ... ],
    "third_party": [ ... ]
  },
  "architecture": {
    "description": "how the pieces fit together",
    "components": [{"name": "", "role": ""}],
    "mermaid": "a valid mermaid 'flowchart TD' diagram of the inferred architecture. CRITICAL: wrap EVERY node label in double quotes, e.g. A[\"PostgreSQL (Supabase)\"] --> B[\"Vercel API\"]. Any label with parentheses, slashes, or punctuation MUST be quoted or the diagram breaks."
  },
  "build_playbook": [{"step": "short title", "detail": "what to do and why", "stack": ["tools"]}],
  "improvements": [{"title": "", "rationale": "", "impact": "high|medium|low", "effort": "high|medium|low"}],
  "mvp_clone_spec": {"pitch": "", "core_features": [""], "recommended_stack": [""], "build_difficulty": 5, "market_potential": "$$", "milestones": [{"name": "", "outcome": ""}]},
  "comparables": [{"name": "", "what": "one line on what they are", "note": "positioning/traction if you know it"}],
  "clone_studio": {
    "stack": ["the concrete stack a builder should use to clone this"],
    "file_tree": "a realistic starter project tree as a plain-text block (dirs + key files)",
    "key_files": [{"path": "path/to/file", "purpose": "what it does"}],
    "setup": ["ordered shell commands to scaffold and run it"],
    "env": ["ENV_VARS the clone would need"],
    "first_feature": "the single first feature to build end-to-end to prove the clone"
  },
  "caveats": ["what you could NOT determine and why"]
}

More guidance:
- comparables: 2-3 real competing/alternative products (name them), so the builder sees the landscape. If unsure of traction, say so in note.
- clone_studio: make it genuinely buildable — a real file tree, real setup commands (npm/pip/etc.), real env vars. This is a from-scratch clone brief, NOT the target's private code.
- infra/hosting labels: a CDN/proxy (Cloudflare seen only via `server: cloudflare`/`cf-ray`, Fastly, CloudFront) is the EDGE in front of the app, NOT where it is hosted. Do NOT call it "Cloudflare Pages" or a host unless a `*.pages.dev` hostname or a Pages-specific header is actually in evidence. When a host or app-builder is detected (e.g. Lovable via a `*.l5e.io`/lovable hostname, Vercel, Netlify, Replit), name THAT as the host/platform and describe Cloudflare/Fastly/etc. as the CDN in front. Keep CDN and host as separate, correctly-labeled entries.

Scoring guidance:
- composite_score (0-100): your overall read = how well-understood the build is AND how clonable it looks. High only when the stack is clear and the app is realistically rebuildable.
- composite_rationale: one sentence justifying that number.
- business_model.type: one of SaaS, B2B, B2C, marketplace, dev-tool, consumer-app, content, ads, other. basis = observed (pricing tiers / login wall / store listing seen) or inferred.
- build_difficulty (integer 1-10): 1 = a weekend clone, 10 = deep technical/infra/data moat.
- market_potential: "$" niche, "$$" solid demand, "$$$" large or clearly proven market.
Output valid JSON only, no prose outside the object."""


CHAT_SYSTEM = """You are BLUEPRINT's analyst. Answer the user's question about THIS specific product
teardown, grounded strictly in the provided evidence and report. Be concrete and technical. If something
was not observed in the signals, say it's inferred or unknown — never fabricate private internals. Keep
answers tight (2-6 sentences or a short list). You may explain how to build or improve the relevant part."""


async def ask(question: str, report: dict, evidence: dict) -> str:
    """Grounded Q&A over a completed teardown (powers 'Ask BLUEPRINT')."""
    context = {
        "target": evidence.get("target"),
        "summary": report.get("summary"),
        "inferred_stack": report.get("inferred_stack"),
        "architecture": report.get("architecture"),
        "business_model": report.get("business_model"),
        "detections": [d.get("name") for d in (evidence.get("detections") or [])][:40],
        "endpoints": (evidence.get("endpoints") or [])[:20],
        "improvements": report.get("improvements"),
        "clone_studio": report.get("clone_studio"),
    }
    user = (f"TEARDOWN CONTEXT:\n{json.dumps(context, ensure_ascii=False)[:7000]}\n\n"
            f"QUESTION: {question.strip()}")
    return await llm.chat([{"role": "system", "content": CHAT_SYSTEM}, {"role": "user", "content": user}],
                          temperature=0.3, max_tokens=800)


BUILD_SYSTEM = """You are a senior full-stack engineer. Using the product teardown below, generate a REAL,
RUNNABLE starter project that clones the product's CORE and bakes in the listed improvements. Build it
FRESH from scratch — NEVER copy proprietary code, assets, text, or branding from the target.

Focus: implement the ONE most important feature end-to-end (clone_studio.first_feature) plus a working UI
shell and all the files needed to actually run it. Prefer the recommended stack. Every file must be COMPLETE
and correct (no "// TODO fill in"). Keep it lean: 6-10 files.

Return ONLY this JSON:
{
  "name": "kebab-case-project-name",
  "summary": "1-2 sentences: what this starter builds and which improvements it includes",
  "stack": ["the stack used"],
  "run": ["ordered shell commands to install and run it"],
  "files": [{"path": "relative/path", "content": "the FULL file contents"}],
  "next_steps": ["what to build next to grow it toward the full product"]
}
Include at minimum: a README.md (with run steps), a manifest (package.json / requirements.txt), the app
entry point, the first-feature implementation, and any config. Output valid JSON only."""


GOAL_DIRECTIVES = {
    "clone": "GOAL: Build a fresh, independent app inspired by the product's CORE — a new project, your own name.",
    "improve": "GOAL: You are rebuilding THIS app FOR ITS OWNER (a friend/classmate asked for help). KEEP its name, "
               "purpose and identity. IMPROVE it: apply the listed improvements, strengthen weak areas, add polish, "
               "and modernize the implementation. It should feel like a better version of THEIR app, not a new one.",
    "fix": "GOAL: You are fixing THIS app FOR ITS OWNER. KEEP its name, purpose and identity. Correct likely bugs, "
           "handle errors and edge cases, and make it robust and reliably runnable — a solid, working version of their app.",
    "redesign": "GOAL: You are redesigning THIS app FOR ITS OWNER. KEEP its name, purpose, features and identity, but "
                "deliver a clean, modern, polished UI/UX overhaul with better layout, styling and usability.",
}


async def generate_app(report: dict, evidence: dict, keep_name: str | None = None, goal: str = "clone") -> dict:
    """AI-build: turn the teardown into a runnable starter project (files) — the 'Build it with AI' button.

    goal: clone | improve | fix | redesign. keep_name: the owner's existing app name to preserve (for rebuilding
    an app a friend/classmate built, keeping their identity)."""
    directive = GOAL_DIRECTIVES.get(goal, GOAL_DIRECTIVES["clone"])
    if keep_name:
        directive += (f'\nUSE THIS EXACT app/project name (the owner already uses it): "{keep_name}". '
                      f'Keep their branding and identity — do NOT rename it.')
    system = BUILD_SYSTEM + "\n\n" + directive
    ctx = {
        "target": evidence.get("target"),
        "summary": report.get("summary"),
        "business_model": report.get("business_model"),
        "recommended_stack": (report.get("clone_studio") or {}).get("stack"),
        "clone_studio": report.get("clone_studio"),
        "mvp_clone_spec": report.get("mvp_clone_spec"),
        "improvements": (report.get("improvements") or [])[:3],
        "inferred_stack": report.get("inferred_stack"),
    }
    user = "PRODUCT TEARDOWN:\n" + json.dumps(ctx, ensure_ascii=False)[:8000]
    reply = await llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                           temperature=0.3, max_tokens=8000, json_mode=True)
    try:
        proj = llm.extract_json(reply)
    except ValueError:
        return {"error": "code generation returned unparseable output", "raw": reply[:1500]}
    # normalize + guard: keep it to a sane file count / size
    files = [f for f in (proj.get("files") or []) if isinstance(f, dict) and f.get("path")][:12]
    for f in files:
        f["content"] = str(f.get("content") or "")[:20000]
    proj["files"] = files
    if keep_name:
        proj["name"] = keep_name           # preserve the owner's name no matter what the model returned
    proj["_goal"] = goal
    return proj


REBUILD_SYSTEM = """You are BLUEPRINT's Rebuild Kit generator. From the product teardown, produce TWO
artifacts a builder pastes into an AI coding agent (Claude / ChatGPT / Cursor) to rebuild a COMPARABLE app
the smart, spec-driven way:
  1) a PRODUCT REQUIREMENTS DOCUMENT (PRD) in clean Markdown, and
  2) a ready-to-paste REBUILD PROMPT that tells the agent to build the app FROM that PRD.

Clean-room ONLY: a fresh, comparable product inferred from PUBLIC signals — NEVER copy the target's code,
assets, copy, or branding. Ground features and stack in the teardown; don't invent proprietary internals.

Return ONLY this JSON:
{
  "name": "a working kebab-case name for the rebuild (NOT the target's brand)",
  "prd": "a COMPLETE PRD in Markdown with these sections, in order: a '# <Name> - PRD' title, then '## Overview', '## Target users', '## Goals & success metrics', '## Core features (MVP)' (numbered, each with a one-line acceptance criterion), '## Later (nice-to-have)', '## Recommended tech stack', '## Architecture (brief)', '## Data model (key entities)', '## Milestones', '## Out of scope & clean-room notes'. Keep it concrete and grounded in the teardown.",
  "prompt": "a single copy-paste prompt (<=250 words) instructing an AI coding agent to build this app SPEC-DRIVEN from the PRD: FIRST restate its understanding and ask any clarifying questions, THEN scaffold the recommended stack, implement the FIRST feature end-to-end, THEN proceed milestone by milestone. Say it must be clean-room (no proprietary copying) and keep every step runnable."
}
Output valid JSON only, no prose outside the object."""


async def rebuild_kit(report: dict, evidence: dict, goal: str = "clone") -> dict:
    """Produce a spec-driven Rebuild Kit — a PRD + a copy-paste rebuild prompt — from a teardown.
    The 'PRD first, then prompt' workflow builds the right thing and wastes fewer tokens."""
    directive = GOAL_DIRECTIVES.get(goal, GOAL_DIRECTIVES["clone"])
    ctx = {
        "target": evidence.get("target"), "host": evidence.get("host"),
        "summary": report.get("summary"),
        "business_model": report.get("business_model"),
        "inferred_stack": report.get("inferred_stack"),
        "architecture": (report.get("architecture") or {}).get("description"),
        "mvp_clone_spec": report.get("mvp_clone_spec"),
        "clone_studio": report.get("clone_studio"),
        "improvements": (report.get("improvements") or [])[:4],
        "comparables": report.get("comparables"),
    }
    user = "PRODUCT TEARDOWN:\n" + json.dumps(ctx, ensure_ascii=False)[:8000]
    reply = await llm.chat([{"role": "system", "content": REBUILD_SYSTEM + "\n\n" + directive},
                            {"role": "user", "content": user}],
                           temperature=0.3, max_tokens=4500, json_mode=True)
    try:
        kit = llm.extract_json(reply)
    except ValueError:
        return {"error": "rebuild kit returned unparseable output", "raw": reply[:1200]}
    return {"name": str(kit.get("name") or "rebuild")[:80], "prd": str(kit.get("prd") or ""),
            "prompt": str(kit.get("prompt") or ""), "_goal": goal}


VOC_SYSTEM = """You are BLUEPRINT's Voice-of-Customer analyst. You are given REAL, PUBLIC app-store reviews of
a product (weighted toward the critical ones). Mine them for what users actually complain about and wish for,
and turn each into a concrete way a rebuild could BEAT the original.

HARD rules (BLUEPRINT's grounding ethos):
- Use ONLY what the reviews actually say. Quote a real snippet for each complaint. NEVER invent a complaint,
  a quote, a number, or a feature nobody mentioned. If the reviews are thin, say so and return fewer items.
- Report how many reviews raise each theme honestly (exact when small, else "several"/"many").
- A "differentiator" is a specific, buildable improvement that directly answers the complaint.

Return STRICT JSON:
{
  "overall_sentiment": "one line - the gist of how users feel",
  "summary": "2-3 sentences on the biggest opportunities a rebuild should target",
  "top_complaints": [
    {"theme": "short label", "frequency": "count or qualitative", "severity": 1,
     "example_quote": "a real snippet from the reviews", "differentiator": "how YOUR rebuild beats this"}
  ],
  "loved": ["things users praise that a rebuild must KEEP (grounded in reviews)"]
}
Order top_complaints by severity x frequency, most actionable first. 4-8 complaints is ideal."""


async def voice_of_customer(reviews_payload: dict) -> dict:
    """Mine PUBLIC app-store reviews into ranked complaints + 'beat-it-here' differentiators, grounded ONLY
    in the supplied reviews (BLUEPRINT red lines: public signals, no fabrication, quote what's really there)."""
    revs = reviews_payload.get("reviews") or []
    if not revs:
        return {"error": "no reviews found to analyze", "note": reviews_payload.get("note", "")}
    low = [r for r in revs if (r.get("rating") or 5) <= 2]        # critical reviews = the complaint gold
    rest = [r for r in revs if (r.get("rating") or 5) > 2]
    picked = (low + rest)[:60]
    compact = [{"stars": r.get("rating"), "t": (r.get("title") or ""),
                "b": (r.get("text") or "")[:400], "src": r.get("source")} for r in picked]
    meta = {"query": reviews_payload.get("query"), "counts": reviews_payload.get("counts"),
            "sources": reviews_payload.get("sources")}
    user = ("PRODUCT: " + json.dumps(meta, ensure_ascii=False)
            + "\n\nPUBLIC REVIEWS (stars, title, body):\n" + json.dumps(compact, ensure_ascii=False)[:9000])
    reply = await llm.chat([{"role": "system", "content": VOC_SYSTEM}, {"role": "user", "content": user}],
                           temperature=0.2, max_tokens=2600, json_mode=True)
    try:
        voc = llm.extract_json(reply)
    except ValueError:
        return {"error": "voice-of-customer returned unparseable output", "raw": reply[:1200]}
    voc["_meta"] = meta
    return voc


def evidence_score(ev: dict) -> dict:
    """Deterministic 0-100 score of how much HARD public evidence we actually gathered — computed from
    the signals, never guessed by the LLM. This is BLUEPRINT's honesty gauge: a confident-looking
    teardown built on thin evidence should say so. Returns {score, basis:[reasons]}."""
    pts = 0
    basis = []
    dets = ev.get("detections", [])
    high = sum(1 for d in dets if d.get("confidence") == "high")
    med = sum(1 for d in dets if d.get("confidence") == "medium")
    if high:
        p = min(30, high * 10); pts += p; basis.append(f"{high} strong fingerprint(s) (+{p})")
    if med:
        p = min(15, med * 3); pts += p; basis.append(f"{med} medium fingerprint(s) (+{p})")
    net = ev.get("network", {})
    if net.get("ip"):
        pts += 6; basis.append("DNS/IP resolved (+6)")
    if net.get("tls_issuer"):
        pts += 6; basis.append("TLS certificate (+6)")
    eps = ev.get("endpoints", [])
    if eps:
        p = min(12, len(eps) * 2); pts += p; basis.append(f"{len(eps)} API endpoint(s) in markup (+{p})")
    if ev.get("github"):
        pts += 14; basis.append("public GitHub org + languages (+14)")
    if ev.get("pricing"):
        pts += 8; basis.append("pricing signals (+8)")
    tph = ev.get("third_party_hosts", [])
    if tph:
        p = min(9, len(tph)); pts += p; basis.append(f"{len(tph)} third-party host(s) (+{p})")
    if (ev.get("active_graphql") or {}).get("introspection") == "OPEN":
        pts += 10; basis.append("open GraphQL introspection (+10)")
    return {"score": min(100, pts), "basis": basis}


AUDIT_SYSTEM = """You are BLUEPRINT's Runtime Health analyst. You are given MEASURED findings from a REAL
headless-browser audit of a live web app (console errors, failed network requests, axe-core accessibility
violations, performance timing, mobile tap targets, and a walk through the app's own views). These are
FACTS captured at runtime — not guesses.

Your job: turn the measurements into a crisp, prioritized fix plan a developer can act on TODAY.

HARD rules (BLUEPRINT's grounding ethos):
- Use ONLY the measured findings provided. NEVER invent an error, a metric, a number, or a success target
  that isn't in the data. If a finding has no evidence, don't list it.
- Every recommendation must tie to a specific measured finding. Reference the real numbers/URLs.
- Be a senior engineer: name the LIKELY root cause and the concrete fix, not generic advice. (e.g. repeated
  failed calls to one API host = a dead/misconfigured backend, not "9 separate bugs".)
- Honest severity. Don't inflate. If the app is healthy, say so.

Return STRICT JSON:
{
  "summary": "2-3 sentences: the real health of this app and the single most important thing to fix",
  "verdict": "healthy | minor-issues | needs-work | broken",
  "root_causes": ["where several findings share ONE underlying cause, name it once"],
  "prioritized_fixes": [
    {"title": "", "severity": "high|medium|low", "category": "",
     "problem": "the measured problem, with the real evidence", "likely_cause": "",
     "fix": "the concrete change to make", "effort": "S|M|L"}
  ],
  "roadmap": {
    "quick_wins": ["<1 day fixes, tied to findings"],
    "larger": ["bigger changes"],
    "strategic": ["optional longer-term"]
  },
  "what_we_measured": ["one line per category actually checked, so the reader trusts the scope"]
}
Order prioritized_fixes by severity then impact. Output valid JSON only, no prose outside the object."""


async def audit_report(audit_data: dict) -> dict:
    """Turn MEASURED runtime-audit findings into a grounded, prioritized fix plan (the honesty edge over
    tools that invent metrics — every line here traces to a real browser measurement)."""
    findings = audit_data.get("findings") or []
    if not findings:
        return {"summary": "No runtime issues were measured — the app loaded cleanly with no console errors, "
                           "failed requests, serious accessibility violations, or broken views.",
                "verdict": "healthy", "root_causes": [], "prioritized_fixes": [], "roadmap": {},
                "what_we_measured": ["console errors", "network requests", "accessibility (axe-core)",
                                     "performance timing", "mobile tap targets", "in-app view walk"]}
    ctx = {
        "target": audit_data.get("target"),
        "health_score": (audit_data.get("health") or {}).get("score"),
        "findings": findings,
        "views_walked": [v for v in (audit_data.get("views") or []) if not v.get("ok")][:10],
        "views_total": len(audit_data.get("views") or []),
    }
    user = "MEASURED RUNTIME AUDIT:\n" + json.dumps(ctx, ensure_ascii=False)[:9000]
    reply = await llm.chat([{"role": "system", "content": AUDIT_SYSTEM}, {"role": "user", "content": user}],
                           temperature=0.2, max_tokens=3000, json_mode=True)
    try:
        rep = llm.extract_json(reply)
    except ValueError:
        return {"error": "audit synthesis returned unparseable output", "raw": reply[:1200]}
    return rep


def _trim(evidence: dict) -> dict:
    """Keep the evidence payload compact for the model."""
    ev = dict(evidence)
    if isinstance(ev.get("headers"), dict):
        ev["headers"] = {k: v[:160] for k, v in ev["headers"].items()}
    if ev.get("pricing") and isinstance(ev["pricing"], dict):
        ev["pricing"] = {k: (v[:400] if isinstance(v, str) else v) for k, v in ev["pricing"].items()}
    return ev


async def synthesize(evidence: dict) -> dict:
    user = ("Here are the collected PUBLIC signals for the target. Produce the teardown JSON.\n\n"
            + json.dumps(_trim(evidence), ensure_ascii=False, indent=2))
    reply = await llm.chat(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        temperature=0.25, max_tokens=6500, json_mode=True,
    )
    try:
        report = llm.extract_json(reply)
    except ValueError:
        return {"summary": "Synthesis returned unparseable output.", "confidence_overall": "low",
                "raw": reply[:2000], "caveats": ["LLM did not return valid JSON."]}
    report["evidence_score"] = evidence_score(evidence)   # deterministic, from real signals
    report["_meta"] = {"target": evidence.get("target"), "kind": evidence.get("kind"),
                       "detections_count": len(evidence.get("detections", []))}
    return report
