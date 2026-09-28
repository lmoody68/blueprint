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
  "caveats": ["what you could NOT determine and why"]
}

Scoring guidance:
- composite_score (0-100): your overall read = how well-understood the build is AND how clonable it looks. High only when the stack is clear and the app is realistically rebuildable.
- composite_rationale: one sentence justifying that number.
- business_model.type: one of SaaS, B2B, B2C, marketplace, dev-tool, consumer-app, content, ads, other. basis = observed (pricing tiers / login wall / store listing seen) or inferred.
- build_difficulty (integer 1-10): 1 = a weekend clone, 10 = deep technical/infra/data moat.
- market_potential: "$" niche, "$$" solid demand, "$$$" large or clearly proven market.
Output valid JSON only, no prose outside the object."""


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
        temperature=0.25, max_tokens=5000, json_mode=True,
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
