"""Tech-stack signature database + matcher (Wappalyzer-style, from PUBLIC signals only).

Each signature detects a technology from things a browser already sees: response headers,
cookies, <script>/<link> URLs, inline HTML, or the <meta generator> tag. No decompilation,
no private data — just fingerprinting observable surface. Matching a signature yields an
evidence string so every downstream claim can cite where it came from.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field


@dataclass
class Sig:
    name: str
    category: str
    # matcher lists; any hit counts. Regexes are case-insensitive.
    headers: list[tuple[str, str]] = field(default_factory=list)   # (header_name, value_regex or "")
    cookies: list[str] = field(default_factory=list)               # cookie name substrings
    scripts: list[str] = field(default_factory=list)               # regex over script/link src
    html: list[str] = field(default_factory=list)                  # regex over body
    generator: list[str] = field(default_factory=list)             # regex over <meta generator>
    implies: list[str] = field(default_factory=list)               # names implied by this hit


SIGNATURES: list[Sig] = [
    # --- Frontend frameworks ---
    Sig("Next.js", "framework", headers=[("x-powered-by", "next\\.js")],
        scripts=[r"/_next/static/"], html=[r'id="__next"', r'"buildId"'], implies=["React", "Node.js"]),
    Sig("Nuxt.js", "framework", scripts=[r"/_nuxt/"], html=[r'id="__nuxt"', r"window\.__NUXT__"], implies=["Vue.js", "Node.js"]),
    Sig("React", "js-framework", scripts=[r"react(-dom)?(\.production)?(\.min)?\.js"], html=[r"data-reactroot", r"__REACT_DEVTOOLS"]),
    Sig("Vue.js", "js-framework", scripts=[r"vue(\.runtime)?(\.global)?(\.min)?\.js"], html=[r"data-v-[0-9a-f]{8}", r"__vue__"]),
    Sig("Angular", "js-framework", scripts=[r"(runtime|polyfills|main)\.[0-9a-f]+\.js"], html=[r"ng-version=", r"_nghost"]),
    Sig("Svelte / SvelteKit", "js-framework", scripts=[r"/_app/immutable/"], html=[r"svelte-[0-9a-z]+"]),
    Sig("Vite", "build-tool", scripts=[r"/assets/index-[0-9A-Za-z_]+\.js", r"/@vite/"], html=[r'type="module".*?/assets/']),
    Sig("jQuery", "js-lib", scripts=[r"jquery[-.]?[0-9.]*(\.min)?\.js"]),
    Sig("Alpine.js", "js-lib", scripts=[r"alpine(\.min)?\.js"], html=[r"x-data="]),
    Sig("HTMX", "js-lib", scripts=[r"htmx(\.org)?(\.min)?\.js"], html=[r"hx-(get|post|target)="]),
    # --- CSS ---
    Sig("Tailwind CSS", "css", scripts=[r"tailwind"],
        html=[r'class="[^"]*\b(?:(?:sm|md|lg|xl):[a-z][\w-]+|[mp][xytblr]?-\d{1,2}|(?:bg|text|border|ring)-\w+-\d{2,3}|flex-(?:col|row)|items-center|justify-(?:center|between))\b']),
    Sig("Bootstrap", "css", scripts=[r"bootstrap(\.min)?\.(css|js)", r"cdn\.jsdelivr\.net/npm/bootstrap"],
        html=[r'class="[^"]*\b(?:col-(?:sm|md|lg|xl)-\d{1,2}|navbar-(?:expand|toggler|brand)|btn btn-(?:primary|secondary|success|danger|outline)|data-bs-)\b']),
    # --- CMS / site builders ---
    Sig("WordPress", "cms", scripts=[r"/wp-content/", r"/wp-includes/"], generator=[r"WordPress"], html=[r"wp-json"]),
    Sig("Shopify", "ecommerce", scripts=[r"cdn\.shopify\.com", r"shopify"], headers=[("x-shopify-stage", "")], html=[r"Shopify\.theme"]),
    Sig("Wix", "site-builder", scripts=[r"static\.parastorage\.com", r"wix"], headers=[("x-wix-request-id", "")]),
    Sig("Squarespace", "site-builder", scripts=[r"squarespace"], generator=[r"Squarespace"]),
    Sig("Webflow", "site-builder", scripts=[r"webflow"], generator=[r"Webflow"], html=[r"data-wf-page"]),
    Sig("Framer", "site-builder", scripts=[r"framerusercontent\.com", r"framer"], html=[r"__framer"]),
    Sig("Ghost", "cms", generator=[r"Ghost"], scripts=[r"/ghost/"]),
    # --- CDN / edge / hosting ---
    Sig("Cloudflare", "cdn", headers=[("server", "cloudflare"), ("cf-ray", ""), ("cf-cache-status", "")]),
    Sig("Vercel", "hosting", headers=[("server", "vercel"), ("x-vercel-id", "")]),
    Sig("Netlify", "hosting", headers=[("server", "netlify"), ("x-nf-request-id", "")]),
    Sig("AWS CloudFront", "cdn", headers=[("x-amz-cf-id", ""), ("via", "cloudfront")]),
    Sig("Fastly", "cdn", headers=[("x-served-by", "cache"), ("x-fastly-request-id", "")]),
    Sig("AWS S3", "hosting", headers=[("server", "AmazonS3"), ("x-amz-request-id", "")]),
    Sig("Google Cloud", "hosting", headers=[("server", "Google Frontend"), ("x-cloud-trace-context", "")]),
    Sig("GitHub Pages", "hosting", headers=[("server", "GitHub\\.com")]),
    # --- Servers / languages ---
    Sig("Nginx", "server", headers=[("server", "nginx")]),
    Sig("Apache", "server", headers=[("server", "apache")]),
    Sig("Microsoft IIS", "server", headers=[("server", "iis"), ("x-aspnet-version", "")]),
    Sig("Express", "server", headers=[("x-powered-by", "express")], implies=["Node.js"]),
    Sig("PHP", "language", headers=[("x-powered-by", "php"), ("set-cookie", "PHPSESSID")]),
    Sig("Ruby on Rails", "framework", headers=[("x-powered-by", "rails"), ("server", "puma")], cookies=["_session_id"]),
    Sig("Django", "framework", cookies=["csrftoken", "django"], html=[r"csrfmiddlewaretoken"], implies=["Python"]),
    Sig("Laravel", "framework", cookies=["laravel_session", "XSRF-TOKEN"], implies=["PHP"]),
    Sig("ASP.NET", "framework", headers=[("x-aspnet-version", ""), ("x-powered-by", "asp\\.net")], cookies=["ASP.NET_SessionId"]),
    # --- Analytics / tags / product ---
    Sig("Google Analytics", "analytics", scripts=[r"google-analytics\.com/analytics\.js", r"gtag/js", r"googletagmanager\.com/gtag"]),
    Sig("Google Tag Manager", "tag-manager", scripts=[r"googletagmanager\.com/gtm\.js"], html=[r"GTM-[A-Z0-9]+"]),
    Sig("Segment", "analytics", scripts=[r"cdn\.segment\.com"]),
    Sig("Mixpanel", "analytics", scripts=[r"cdn\.mxpnl\.com", r"mixpanel"]),
    Sig("Amplitude", "analytics", scripts=[r"amplitude"]),
    Sig("Hotjar", "analytics", scripts=[r"static\.hotjar\.com"]),
    Sig("Plausible", "analytics", scripts=[r"plausible\.io/js"]),
    Sig("PostHog", "analytics", scripts=[r"posthog"]),
    # --- Payments / support / infra services ---
    Sig("Stripe", "payments", scripts=[r"js\.stripe\.com"]),
    Sig("PayPal", "payments", scripts=[r"paypal\.com/sdk", r"paypalobjects"]),
    Sig("Intercom", "support", scripts=[r"widget\.intercom\.io", r"intercomcdn"]),
    Sig("Zendesk", "support", scripts=[r"zendesk", r"zdassets"]),
    Sig("HubSpot", "marketing", scripts=[r"js\.hs-scripts\.com", r"hsubspot|hubspot"]),
    Sig("Drift", "support", scripts=[r"js\.driftt\.com"]),
    Sig("Sentry", "error-tracking", scripts=[r"sentry-cdn\.com", r"@sentry/"], html=[r"Sentry\.init"]),
    Sig("Cloudinary", "media", scripts=[r"res\.cloudinary\.com"]),
    Sig("Algolia", "search", scripts=[r"algolia"]),
    Sig("Firebase", "backend", scripts=[r"firebaseio\.com", r"firebase(app)?\.js", r"gstatic\.com/firebasejs"]),
    Sig("Supabase", "backend", scripts=[r"supabase"], headers=[("x-supabase", "")]),
    Sig("Contentful", "cms", scripts=[r"contentful"]),
    Sig("Sanity", "cms", scripts=[r"sanity"]),
]


def _search_headers(headers: dict, name: str, pattern: str) -> str | None:
    val = headers.get(name.lower())
    if val is None:
        return None
    if pattern == "":
        return f"header `{name}: {val[:80]}`"
    if re.search(pattern, val, re.I):
        return f"header `{name}: {val[:80]}`"
    return None


def match(headers: dict, cookies: list[str], scripts: list[str], body: str,
          generator: str | None) -> list[dict]:
    """Run every signature; return detections with evidence + implied technologies."""
    headers_lc = {k.lower(): v for k, v in headers.items()}
    body_l = body or ""
    detected: dict[str, dict] = {}

    def add(name: str, category: str, evidence: str, confidence: str = "medium"):
        if name not in detected:
            detected[name] = {"name": name, "category": category, "confidence": confidence, "evidence": evidence}

    for sig in SIGNATURES:
        ev = None
        for hn, hp in sig.headers:
            ev = _search_headers(headers_lc, hn, hp)
            if ev:
                break
        if not ev:
            for cname in sig.cookies:
                if any(cname.lower() in c.lower() for c in cookies):
                    ev = f"cookie `{cname}`"
                    break
        if not ev:
            for sp in sig.scripts:
                for s in scripts:
                    if re.search(sp, s, re.I):
                        ev = f"asset `{s[:90]}`"
                        break
                if ev:
                    break
        if not ev and generator:
            for gp in sig.generator:
                if re.search(gp, generator, re.I):
                    ev = f"meta generator `{generator[:60]}`"
                    break
        if not ev:
            for hp in sig.html:
                if re.search(hp, body_l, re.I):
                    ev = f"markup pattern `{hp}`"
                    break
        if ev:
            # header/cookie/generator hits are strong; html-pattern hits are weaker
            conf = "high" if (sig.headers or sig.cookies or sig.generator) and "markup pattern" not in ev else "medium"
            add(sig.name, sig.category, ev, conf)
            for imp in sig.implies:
                add(imp, "implied", f"implied by {sig.name}", "low")

    return list(detected.values())
