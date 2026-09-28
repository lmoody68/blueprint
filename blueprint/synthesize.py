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
    "mermaid": "a valid mermaid 'flowchart TD' diagram string of the inferred architecture"
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
