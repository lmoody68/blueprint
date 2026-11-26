"""BLUEPRINT — Runtime Health Audit (the headless-browser layer).

BLUEPRINT's teardown is static-fetch (httpx/BS4): it sees the shipped HTML, never the LIVE app. This module
adds the missing dimension — it loads the target in a REAL headless browser (Playwright/Chromium) and MEASURES
what actually happens at runtime:

  • JavaScript console errors + uncaught page errors
  • failed network requests (dead APIs, 4xx/5xx, blocked CORS)   <- how we caught NOVAS's dead backend
  • accessibility violations (axe-core, WCAG)                     <- real, not guessed
  • performance timing + heaviest resources (navigation timing)
  • mobile tap targets < 44px
  • page basics (title, meta description, single h1, lang, viewport)
  • SPA VIEW WALK — clicks through in-app nav and audits each view (Conferra only ever sees one page)

Everything here is MEASURED fact, tagged [measured]. No fabrication — that's the honesty edge. Read-only:
it navigates and inspects public pages, never submits forms, buys, or writes (mirrors BLUEPRINT's red lines).
"""
from __future__ import annotations

import asyncio
import json
import os
import time

from . import collectors

try:
    from playwright.async_api import async_playwright
    _PLAYWRIGHT = True
except Exception:  # pragma: no cover
    _PLAYWRIGHT = False

_VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "axe.min.js")

# Tunables (env-overridable, matching BLUEPRINT's BLUEPRINT_* convention)
NAV_TIMEOUT = int(os.getenv("BLUEPRINT_AUDIT_NAV_MS", "35000"))
SETTLE_MS = int(os.getenv("BLUEPRINT_AUDIT_SETTLE_MS", "3500"))
MAX_VIEWS = int(os.getenv("BLUEPRINT_AUDIT_MAX_VIEWS", "8"))
MAX_PAGES = int(os.getenv("BLUEPRINT_AUDIT_MAX_PAGES", "4"))   # distinct same-origin URLs to crawl + audit
TAP_MIN = 44
EXECUTABLE = os.getenv("BLUEPRINT_AUDIT_CHROME", "")  # optional system Chrome path (deploy)

_AXE_JS = ""
def _axe() -> str:
    global _AXE_JS
    if not _AXE_JS:
        try:
            with open(_VENDOR, "r", encoding="utf-8") as f:
                _AXE_JS = f.read()
        except Exception:
            _AXE_JS = ""
    return _AXE_JS


# ── per-page measurement (runs inside the browser) ───────────────────────────────────────────────
_MEASURE_JS = r"""
(() => {
  const out = {};
  // basics
  out.title = (document.title || '').slice(0, 160);
  out.metaDescription = !!document.querySelector('meta[name="description"]');
  out.lang = document.documentElement.getAttribute('lang') || '';
  out.viewportMeta = !!document.querySelector('meta[name="viewport"]');
  out.h1Count = document.querySelectorAll('h1').length;
  // images missing alt
  const imgs = [...document.querySelectorAll('img')];
  out.imgCount = imgs.length;
  out.imgNoAlt = imgs.filter(i => i.getAttribute('alt') === null).length;
  // performance (navigation timing)
  try {
    const nav = performance.getEntriesByType('navigation')[0];
    const paints = performance.getEntriesByType('paint');
    const fcp = (paints.find(p => p.name === 'first-contentful-paint') || {}).startTime || null;
    out.perf = nav ? {
      domContentLoaded: Math.round(nav.domContentLoadedEventEnd),
      loadComplete: Math.round(nav.loadEventEnd),
      ttfb: Math.round(nav.responseStart),
      fcp: fcp ? Math.round(fcp) : null,
      transferKB: Math.round((performance.getEntriesByType('resource').reduce((s,r)=>s+(r.transferSize||0),0) + (nav.transferSize||0))/1024),
      requests: performance.getEntriesByType('resource').length + 1,
    } : null;
    // heaviest resources
    out.heavy = performance.getEntriesByType('resource')
      .map(r => ({ url: (r.name||'').slice(0,120), kb: Math.round((r.transferSize||0)/1024), type: r.initiatorType }))
      .filter(r => r.kb > 0).sort((a,b)=>b.kb-a.kb).slice(0,6);
  } catch(e) { out.perf = null; out.heavy = []; }
  // horizontal overflow (content wider than the viewport → sideways scroll, esp. on mobile)
  try {
    const de = document.documentElement;
    out.scrollWidth = de.scrollWidth; out.innerWidth = window.innerWidth;
    out.overflowX = de.scrollWidth > window.innerWidth + 4;
    if (out.overflowX) {
      // find the widest offending elements
      const vw = window.innerWidth; const wide = [];
      for (const el of document.querySelectorAll('body *')) {
        const r = el.getBoundingClientRect();
        if (r.right > vw + 4 && r.width > 40) wide.push({ w: Math.round(r.width), right: Math.round(r.right),
          tag: el.tagName.toLowerCase(), cls: (el.className||'').toString().slice(0,30) });
      }
      out.overflowCulprits = wide.sort((a,b)=>b.right-a.right).slice(0,4);
    }
  } catch(e) {}
  // mixed content (http sub-resources on https page)
  if (location.protocol === 'https:') {
    out.mixedContent = performance.getEntriesByType('resource')
      .map(r => r.name).filter(u => u.startsWith('http://')).slice(0,8);
  } else { out.mixedContent = []; }
  return out;
})()
"""

_TAP_JS = r"""
(() => {
  const els = [...document.querySelectorAll('a, button, [role="button"], input[type="button"], input[type="submit"], select')];
  const small = [];
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none') continue;
    if (r.width < %d || r.height < %d) {
      small.push({ w: Math.round(r.width), h: Math.round(r.height),
        text: (el.textContent || el.getAttribute('aria-label') || '').trim().slice(0,30) });
    }
  }
  return small.slice(0, 40);
})()
""" % (TAP_MIN, TAP_MIN)


async def _run_axe(page) -> list:
    """Inject vendored axe-core and run it; return normalized violations."""
    js = _axe()
    if not js:
        return []
    try:
        await page.add_script_tag(content=js)
        res = await page.evaluate("""async () => {
            try {
              const r = await axe.run(document, { resultTypes: ['violations'],
                runOnly: { type:'tag', values:['wcag2a','wcag2aa','best-practice'] } });
              return r.violations.map(v => ({ id:v.id, impact:v.impact||'minor',
                help:v.help, nodes:v.nodes.length,
                sample:(v.nodes[0]&&v.nodes[0].target&&v.nodes[0].target.join(' '))||'' }));
            } catch(e) { return []; }
        }""")
        return res or []
    except Exception:
        return []


async def _audit_page(context, url: str, label: str, mobile: bool, full: bool) -> dict:
    """Load one URL (or view) and measure it. `full` = also run axe + tap + perf (heavier)."""
    page = await context.new_page()
    console_errors, page_errors, failed = [], [], []
    page.on("console", lambda m: console_errors.append(m.text[:200]) if m.type == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)[:200]))
    page.on("requestfailed", lambda r: failed.append({"url": r.url[:140], "err": (r.failure or "")[:60]}))
    page.on("response", lambda r: failed.append({"url": r.url[:140], "status": r.status}) if r.status >= 400 else None)

    rec = {"label": label, "url": url, "mobile": mobile}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    except Exception as e:
        rec["nav_error"] = f"{type(e).__name__}: {str(e)[:120]}"
    await page.wait_for_timeout(SETTLE_MS)

    if full:
        try:
            rec["measure"] = await page.evaluate(_MEASURE_JS)
        except Exception:
            rec["measure"] = {}
        rec["axe"] = await _run_axe(page)
        if mobile:
            try:
                rec["tap_small"] = await page.evaluate(_TAP_JS)
            except Exception:
                rec["tap_small"] = []
    # dedupe network failures
    seen, uniq = set(), []
    for f in failed:
        k = f.get("url", "") + str(f.get("status", f.get("err", "")))
        if k not in seen:
            seen.add(k); uniq.append(f)
    rec["console_errors"] = console_errors[:12]
    rec["page_errors"] = page_errors[:8]
    rec["failed_requests"] = uniq[:15]
    await page.close()
    return rec


async def _discover_views(page) -> list:
    """Find in-app nav controls (SPA sidebar/nav/tabs) to walk — the thing Conferra can't do."""
    try:
        return await page.evaluate(r"""
        () => {
          const sels = ['.sidebar__item','[class*="sidebar"] a','[class*="sidebar"] button',
            'nav a','nav button','[role="tab"]','[role="navigation"] a','aside a','aside button',
            '.nav-link','[class*="menu"] a'];
          const seen = new Set(); const items = [];
          for (const sel of sels) {
            for (const el of document.querySelectorAll(sel)) {
              const t = (el.textContent||'').trim().replace(/\s+/g,' ');
              if (t && t.length < 40 && !seen.has(t)) { seen.add(t); items.push(t); }
            }
          }
          return items.slice(0, 40);
        }""") or []
    except Exception:
        return []


async def _discover_links(context, url: str, host: str) -> list:
    """Find distinct same-origin page URLs linked from the landing page (for the multi-page crawl)."""
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        await page.wait_for_timeout(2000)
        hrefs = await page.evaluate("""(host) => {
          const seen = new Set(); const out = [];
          for (const a of document.querySelectorAll('a[href]')) {
            let u; try { u = new URL(a.href, location.href); } catch(e) { continue; }
            if (u.host !== host) continue;                       // same-origin only
            if (!/^https?:/.test(u.protocol)) continue;
            u.hash = '';
            const key = u.pathname.replace(/\\/$/, '');
            if (key === '' || key === location.pathname.replace(/\\/$/,'')) continue;  // skip landing
            if (seen.has(key)) continue; seen.add(key);
            if (/\\.(pdf|zip|png|jpe?g|svg|gif|mp4|webm|css|js|ico)$/i.test(u.pathname)) continue;  // skip assets
            out.push(u.origin + u.pathname);
          }
          return out;
        }""", host)
        return hrefs or []
    except Exception:
        return []
    finally:
        await page.close()


async def _walk_views(context, url: str, labels: list) -> list:
    """Click each in-app nav item and record per-view runtime errors (lightweight)."""
    page = await context.new_page()
    cur = {"errs": [], "fails": []}
    page.on("console", lambda m: cur["errs"].append(m.text[:160]) if m.type == "error" else None)
    page.on("pageerror", lambda e: cur["errs"].append(str(e)[:160]))
    page.on("requestfailed", lambda r: cur["fails"].append(r.url[:120]))
    page.on("response", lambda r: cur["fails"].append(f"{r.status} {r.url[:110]}") if r.status >= 400 else None)
    views = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        await page.wait_for_timeout(2000)
        for label in labels[:MAX_VIEWS]:
            cur["errs"], cur["fails"] = [], []
            clicked = await page.evaluate("""(want) => {
              const all = [...document.querySelectorAll('.sidebar__item,nav a,nav button,[role="tab"],aside a,aside button,a,button')];
              const el = all.find(e => (e.textContent||'').trim().replace(/\\s+/g,' ') === want);
              if (!el) return false; el.click(); return true;
            }""", label)
            if not clicked:
                continue
            await page.wait_for_timeout(1800)
            errs = list(dict.fromkeys(cur["errs"]))[:5]
            fails = list(dict.fromkeys(cur["fails"]))[:5]
            views.append({"view": label, "errors": errs, "failed": fails,
                          "ok": not errs and not fails})
    except Exception as e:
        views.append({"view": "_walk_error", "errors": [f"{type(e).__name__}: {str(e)[:100]}"], "failed": [], "ok": False})
    await page.close()
    return views


async def run_audit(target: str, walk: bool = True) -> dict:
    """Full runtime health audit. Returns MEASURED findings (desktop + mobile landing, SPA view walk)."""
    if not _PLAYWRIGHT:
        return {"error": "Playwright is not installed on this server (runtime audit unavailable)."}
    url = collectors.normalize_url(target)
    host = collectors.host_of(url)
    ok, why = collectors.public_host_ok(host)
    if not ok:
        return {"error": why, "target": target}

    t0 = time.time()
    result = {"target": target, "url": url, "host": host, "pages": [], "views": []}
    launch_kw = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
    if EXECUTABLE:
        launch_kw["executable_path"] = EXECUTABLE
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(**launch_kw)
        except Exception as e:
            return {"error": "Runtime audit unavailable on this server — the headless browser (Chromium) "
                             "isn't installed here. Run it locally, or install with `playwright install chromium`.",
                    "detail": f"{type(e).__name__}: {str(e)[:160]}", "target": target}
        try:
            # desktop full audit. bypass_csp so our axe-core harness can inject on strict-CSP pages
            # (script-src 'self'); otherwise the page's CSP blocks the injection — which both breaks the
            # a11y scan AND shows up as a FALSE "inline script blocked" console error against the target.
            dctx = await browser.new_context(viewport={"width": 1366, "height": 900}, bypass_csp=True,
                                             user_agent="Mozilla/5.0 (BLUEPRINT-Audit; +read-only)")
            result["pages"].append(await _audit_page(dctx, url, "landing (desktop)", mobile=False, full=True))
            # MULTI-URL CRAWL — follow distinct same-origin links and audit a few real routes (a SPA view-walk
            # misses multi-PAGE sites; this is how we catch per-page h1/label/meta issues like Conferra does).
            if walk and MAX_PAGES > 0:
                links = await _discover_links(dctx, url, host)
                for i, lk in enumerate(links[:MAX_PAGES]):
                    result["pages"].append(await _audit_page(dctx, lk, f"page: {lk.split(host,1)[-1][:40] or '/'}",
                                                             mobile=False, full=True))
                if links:
                    result["pages_crawled"] = len(links[:MAX_PAGES]) + 1
            # SPA view discovery + walk (on desktop context)
            if walk:
                disc_page = await dctx.new_page()
                try:
                    await disc_page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                    await disc_page.wait_for_timeout(2500)
                    labels = await _discover_views(disc_page)
                finally:
                    await disc_page.close()
                if labels:
                    result["views"] = await _walk_views(dctx, url, labels)
                    result["views_discovered"] = len(labels)
            await dctx.close()
            # mobile full audit (tap targets)
            mctx = await browser.new_context(viewport={"width": 390, "height": 844}, bypass_csp=True,
                                             is_mobile=True, has_touch=True,
                                             user_agent="Mozilla/5.0 (iPhone; BLUEPRINT-Audit; +read-only)")
            result["pages"].append(await _audit_page(mctx, url, "landing (mobile)", mobile=True, full=True))
            await mctx.close()
        finally:
            await browser.close()

    result["findings"] = _derive_findings(result)
    result["health"] = _health_score(result)
    result["elapsed_ms"] = int((time.time() - t0) * 1000)
    return result


# ── VISUAL STUDY (Craft Anything's "study the pages at every width", made IP-safe) ────────────────
# Loads the PUBLIC page in a real browser at three widths and OBSERVES the rendered result: structure,
# palette, fonts, element counts, and a thumbnail. This is the dimension the static teardown can't see —
# BLUEPRINT now SEES the page like CA does, instead of only inferring from shipped HTML. Read-only.
VISUAL_WIDTHS = [("desktop", 1366, 900), ("tablet", 834, 1112), ("mobile", 390, 844)]

_VISUAL_JS = r"""() => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const txt = el => (el.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 70);
  const q = sel => [...document.querySelectorAll(sel)].filter(vis);
  const headings = q('h1,h2').slice(0, 8).map(h => ({ tag: h.tagName, text: txt(h) }));
  const b = getComputedStyle(document.body);
  const accents = {};
  q('a,button,[role=button],[class*=btn]').slice(0, 60).forEach(e => {
    const c = getComputedStyle(e).backgroundColor;
    if (c && c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent') accents[c] = (accents[c] || 0) + 1; });
  const accent = Object.entries(accents).sort((a, c) => c[1] - a[1]).slice(0, 3).map(x => x[0]);
  return { title: document.title,
    layout: { headings, navs: q('nav').length, sections: q('section,main>div,[class*=section]').length,
              buttons: q('a[class*=btn],button,[role=button]').length,
              inputs: q('input,textarea,select').length, images: q('img').length,
              cards: q('[class*=card]').length },
    palette: { bg: b.backgroundColor, text: b.color, accent,
               font: (b.fontFamily || '').split(',')[0].replace(/["']/g, '').trim() },
    overflow: document.documentElement.scrollWidth > window.innerWidth + 2 };
}"""


async def visual_study(target: str, shots: bool = True) -> dict:
    """CA's 'Study at every width', IP-safe: render the PUBLIC page at desktop/tablet/mobile and OBSERVE its
    structure, palette, fonts and a thumbnail — grounding the teardown/check in how the app actually LOOKS,
    not just its HTML. Read-only; SSRF-guarded; degrades gracefully where the browser isn't installed."""
    if not _PLAYWRIGHT:
        return {"error": "Visual study unavailable — Playwright isn't installed on this server.", "target": target}
    url = collectors.normalize_url(target)
    host = collectors.host_of(url)
    ok, why = collectors.public_host_ok(host)
    if not ok:
        return {"error": why, "target": target}
    import base64
    t0 = time.time()
    launch_kw = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
    if EXECUTABLE:
        launch_kw["executable_path"] = EXECUTABLE
    out = {"target": target, "url": url, "host": host, "widths": []}
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(**launch_kw)
        except Exception as e:
            return {"error": "Visual study unavailable — the headless browser (Chromium) isn't installed here. "
                             "Run it locally, or install with `playwright install chromium`.",
                    "detail": f"{type(e).__name__}: {str(e)[:150]}", "target": target}
        try:
            for name, w, h in VISUAL_WIDTHS:
                ctx = await browser.new_context(viewport={"width": w, "height": h}, bypass_csp=True,
                                                user_agent="Mozilla/5.0 (BLUEPRINT-Study; +read-only)")
                page = await ctx.new_page()
                entry = {"width": name, "px": w}
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                    await page.wait_for_timeout(min(SETTLE_MS, 3000))
                    entry["observed"] = await page.evaluate(_VISUAL_JS)
                    if shots:
                        png = await page.screenshot(type="jpeg", quality=55, full_page=False)
                        entry["thumb"] = "data:image/jpeg;base64," + base64.b64encode(png).decode()
                except Exception as e:  # one width failing never sinks the rest
                    entry["error"] = f"{type(e).__name__}: {str(e)[:120]}"
                out["widths"].append(entry)
                await ctx.close()
        finally:
            await browser.close()
    out["elapsed_ms"] = int((time.time() - t0) * 1000)
    return out


# ── deterministic rollups (no LLM — these are measured facts) ─────────────────────────────────────
def _derive_findings(r: dict) -> list:
    """Roll the raw measurements into deduped, severity-tagged findings — all [measured]."""
    f = []
    desktop = next((p for p in r["pages"] if not p.get("mobile")), {})
    mobile = next((p for p in r["pages"] if p.get("mobile")), {})

    # network failures (any page)
    all_fail = {}
    for p in r["pages"]:
        for x in p.get("failed_requests", []):
            key = x.get("url", "")
            all_fail[key] = x.get("status", x.get("err", "failed"))
    if all_fail:
        f.append({"id": "net", "severity": "high", "category": "reliability",
                  "title": f"{len(all_fail)} failed network request(s)",
                  "detail": "Requests that errored or returned 4xx/5xx at runtime (dead API, CORS, or 404 asset).",
                  "evidence": [f"{v} — {k}" for k, v in list(all_fail.items())[:8]]})

    # console / page errors
    cerr = {}
    for p in r["pages"]:
        for e in p.get("console_errors", []) + p.get("page_errors", []):
            cerr[e] = cerr.get(e, 0) + 1
    if cerr:
        f.append({"id": "jserr", "severity": "high", "category": "reliability",
                  "title": f"{sum(cerr.values())} JavaScript console error(s)",
                  "detail": "Uncaught errors / console.error at runtime — broken functionality or failed calls.",
                  "evidence": list(cerr.keys())[:6]})

    # axe accessibility (merge desktop+mobile by id, keep worst impact)
    order = {"critical": 3, "serious": 2, "moderate": 1, "minor": 0}
    axe = {}
    for p in r["pages"]:
        for v in p.get("axe", []):
            cur = axe.get(v["id"])
            if not cur or order.get(v["impact"], 0) > order.get(cur["impact"], 0):
                axe[v["id"]] = v
    crit = [v for v in axe.values() if v["impact"] in ("critical", "serious")]
    if axe:
        sev = "high" if crit else "medium"
        f.append({"id": "a11y", "severity": sev, "category": "accessibility",
                  "title": f"{len(axe)} accessibility rule(s) failing ({len(crit)} serious+)",
                  "detail": "WCAG violations measured by axe-core (real users on screen readers are affected).",
                  "evidence": [f"[{v['impact']}] {v['help']} ×{v['nodes']}" for v in
                               sorted(axe.values(), key=lambda v: -order.get(v['impact'],0))[:7]]})

    # tap targets (mobile)
    tap = mobile.get("tap_small", [])
    if tap:
        f.append({"id": "tap", "severity": "medium" if len(tap) < 10 else "high", "category": "mobile ux",
                  "title": f"{len(tap)} tap target(s) under {TAP_MIN}px on mobile",
                  "detail": f"Links/buttons smaller than {TAP_MIN}px are hard to tap (WCAG 2.5.5).",
                  "evidence": [f"{t['text'] or '(no text)'} — {t['w']}×{t['h']}px" for t in tap[:8]]})

    # horizontal overflow on mobile (sideways scroll — content wider than the screen)
    mm = mobile.get("measure") or {}
    if mm.get("overflowX"):
        ev = [f"content {mm.get('scrollWidth','?')}px wide on a {mm.get('innerWidth','?')}px screen"]
        ev += [f"{c['tag']}.{c['cls']} extends to {c['right']}px (w={c['w']})" for c in mm.get("overflowCulprits", [])[:3]]
        f.append({"id": "overflowx", "severity": "high", "category": "mobile ux",
                  "title": "Page scrolls sideways on mobile",
                  "detail": "Content is wider than the phone viewport, forcing horizontal scroll — phone users "
                            "struggle to read/tap and many leave. Usually one fixed-width element.",
                  "evidence": ev})

    # performance
    perf = (desktop.get("measure") or {}).get("perf") or {}
    if perf:
        slow = perf.get("loadComplete", 0) > 4000 or perf.get("fcp") and perf["fcp"] > 2500
        heavy = (desktop.get("measure") or {}).get("heavy", [])
        bigpayload = perf.get("transferKB", 0) > 3000
        if slow or bigpayload:
            ev = [f"load {perf.get('loadComplete','?')}ms", f"FCP {perf.get('fcp','?')}ms",
                  f"TTFB {perf.get('ttfb','?')}ms", f"~{perf.get('transferKB','?')}KB over {perf.get('requests','?')} requests"]
            if heavy:
                ev += [f"heavy: {h['kb']}KB {h['type']} {h['url']}" for h in heavy[:3]]
            f.append({"id": "perf", "severity": "medium", "category": "performance",
                      "title": "Slow / heavy page load",
                      "detail": "Measured navigation timing indicates slow first paint or a large transfer.",
                      "evidence": ev})

    # SEO / semantics basics — aggregated across the landing + every crawled page
    basics = []
    desk_pages = [p for p in r["pages"] if not p.get("mobile")]
    for p in desk_pages:
        m = p.get("measure") or {}
        where = p.get("label", "").replace("page: ", "").replace("landing (desktop)", "landing")
        if m.get("h1Count", 0) == 0: basics.append(f"no <h1> on {where}")
        elif m.get("h1Count", 0) > 1: basics.append(f"{m['h1Count']} <h1> on {where} (should be 1)")
        if not m.get("metaDescription"): basics.append(f"no <meta description> on {where}")
        if m.get("imgNoAlt"): basics.append(f"{m['imgNoAlt']} image(s) missing alt on {where}")
    m0 = desktop.get("measure") or {}
    if not m0.get("lang"): basics.append("no <html lang>")
    if not m0.get("viewportMeta"): basics.append("no responsive viewport meta")
    if basics:
        f.append({"id": "basics", "severity": "low", "category": "seo/semantics",
                  "title": f"{len(basics)} page-hygiene issue(s)",
                  "detail": "Basic SEO/semantics/meta hygiene gaps.",
                  "evidence": basics})

    # SPA views that errored
    bad_views = [v for v in r.get("views", []) if not v.get("ok")]
    if bad_views:
        f.append({"id": "views", "severity": "high", "category": "reliability",
                  "title": f"{len(bad_views)} of {len(r.get('views', []))} app view(s) throw errors",
                  "detail": "Walking the in-app navigation, these views logged runtime errors or failed requests "
                            "(the kind of per-view breakage a single-page audit never sees).",
                  "evidence": [f"{v['view']}: " + "; ".join((v['errors'] + v['failed'])[:2]) for v in bad_views[:8]]})

    sev_rank = {"high": 0, "medium": 1, "low": 2}
    return sorted(f, key=lambda x: sev_rank.get(x["severity"], 3))


def _health_score(r: dict) -> dict:
    """Deterministic 0-100 health score from the measured findings (BLUEPRINT's honesty-as-a-number)."""
    score = 100
    basis = []
    for f in r.get("findings", []):
        pass  # findings derived after; recompute from pages below
    # recompute directly from raw to avoid ordering dependency
    net = sum(len(p.get("failed_requests", [])) for p in r["pages"])
    js = sum(len(p.get("console_errors", [])) + len(p.get("page_errors", [])) for p in r["pages"])
    axe_ct = len({v["id"] for p in r["pages"] for v in p.get("axe", [])})
    axe_serious = len({v["id"] for p in r["pages"] for v in p.get("axe", []) if v["impact"] in ("critical", "serious")})
    tap = sum(len(p.get("tap_small", [])) for p in r["pages"])
    overflow = any((p.get("measure") or {}).get("overflowX") for p in r["pages"])
    bad_views = len([v for v in r.get("views", []) if not v.get("ok")])
    if net:
        d = min(30, net * 6); score -= d; basis.append(f"-{d} {net} failed request(s)")
    if js:
        d = min(25, js * 4); score -= d; basis.append(f"-{d} {js} JS error(s)")
    if axe_serious:
        d = min(20, axe_serious * 5); score -= d; basis.append(f"-{d} {axe_serious} serious a11y")
    if axe_ct - axe_serious > 0:
        d = min(8, (axe_ct - axe_serious) * 2); score -= d; basis.append(f"-{d} {axe_ct-axe_serious} minor a11y")
    if tap:
        d = min(10, tap); score -= d; basis.append(f"-{d} {tap} small tap target(s)")
    if overflow:
        score -= 10; basis.append("-10 sideways scroll on mobile")
    if bad_views:
        d = min(20, bad_views * 5); score -= d; basis.append(f"-{d} {bad_views} broken view(s)")
    return {"score": max(0, score), "basis": basis}
