"""Signal collectors for BLUEPRINT — gather PUBLIC, observable evidence about a target.

Red lines enforced here, not just documented:
  * robots.txt is fetched and honored (we skip disallowed paths).
  * Rate-limited, bounded fetches; a normal browser User-Agent (we see what a user sees).
  * No authentication, no login-walled scraping, no binary download/decompilation.
  * "active" probes (endpoint discovery, GraphQL introspection) run ONLY when caller opts in.
"""
from __future__ import annotations
import re
import ssl
import socket
import asyncio
import ipaddress
import urllib.parse
import urllib.robotparser
import httpx

from . import signatures

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 BLUEPRINT/0.1 (+public-signal teardown)")

INTERESTING_HEADERS = ["server", "x-powered-by", "via", "cf-ray", "x-vercel-id", "x-nf-request-id",
                       "x-amz-cf-id", "x-served-by", "content-security-policy", "x-frame-options",
                       "strict-transport-security", "x-aspnet-version", "set-cookie"]


def normalize_url(target: str) -> str:
    target = target.strip()
    if not re.match(r"^https?://", target, re.I):
        target = "https://" + target
    return target


def host_of(url: str) -> str:
    return urllib.parse.urlparse(url).hostname or ""


def public_host_ok(host: str) -> tuple[bool, str]:
    """SSRF GUARD — resolve the target and refuse anything that isn't a public IP.
    BLUEPRINT fetches user-supplied URLs server-side, so without this a caller could aim it at cloud
    metadata (169.254.169.254), localhost, or the internal LAN (192.168/10/172.16) to probe or exfiltrate.
    We block loopback, private, link-local, reserved, multicast, and unspecified addresses (v4 and v6)."""
    if not host:
        return False, "no host in target"
    if host.lower() in ("localhost", "ip6-localhost", "metadata", "metadata.google.internal"):
        return False, f"blocked host '{host}' (SSRF guard)"
    try:
        ips = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except Exception as e:
        return False, f"could not resolve host ({type(e).__name__})"
    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip.split("%")[0])
        except ValueError:
            continue
        if (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
                or addr.is_multicast or addr.is_unspecified):
            return False, f"target resolves to a non-public address ({ip}) — blocked (SSRF guard)"
    return True, "public"


async def _robots_ok(client: httpx.AsyncClient, base: str, path: str = "/") -> tuple[bool, str]:
    robots_url = urllib.parse.urljoin(base, "/robots.txt")
    try:
        r = await client.get(robots_url)
        if r.status_code >= 400:
            return True, "no robots.txt (allowed)"
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(r.text.splitlines())
        allowed = rp.can_fetch(BROWSER_UA, urllib.parse.urljoin(base, path))
        return allowed, ("allowed by robots.txt" if allowed else "DISALLOWED by robots.txt")
    except Exception as e:
        return True, f"robots.txt unreadable ({type(e).__name__}); proceeding on public root only"


def _tls_info(host: str, port: int = 443, timeout: float = 6.0) -> dict:
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
        issuer = dict(x[0] for x in cert.get("issuer", [])).get("organizationName", "?")
        subject = dict(x[0] for x in cert.get("subject", [])).get("commonName", host)
        sans = [v for (k, v) in cert.get("subjectAltName", []) if k == "DNS"]
        return {"tls_issuer": issuer, "tls_subject": subject, "tls_sans": sans[:8], "tls_expires": cert.get("notAfter")}
    except Exception as e:
        return {"tls_error": f"{type(e).__name__}: {e}"}


def _dns_info(host: str) -> dict:
    try:
        ip = socket.gethostbyname(host)
    except Exception as e:
        return {"dns_error": str(e)}
    info = {"ip": ip}
    try:
        info["reverse"] = socket.gethostbyaddr(ip)[0]
    except Exception:
        pass
    return info


def _extract_assets(html: str, base: str) -> list[str]:
    urls = re.findall(r'<(?:script|link)[^>]+(?:src|href)=["\']([^"\']+)["\']', html, re.I)
    out = []
    for u in urls:
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("/"):
            u = urllib.parse.urljoin(base, u)
        out.append(u)
    return out[:200]


def _extract_endpoints(html: str) -> list[str]:
    """Passive: API-ish paths already present in the delivered HTML (no bundle crawling)."""
    found = set()
    for m in re.findall(r'["\'](/(?:api|v1|v2|graphql|rest|rpc)[/\w.\-]*)', html, re.I):
        found.add(m)
    for m in re.findall(r'https?://[\w.\-]*api[\w.\-]*\.\w+[/\w.\-]*', html, re.I):
        found.add(m)
    return sorted(found)[:30]


async def _github_org(client: httpx.AsyncClient, host: str) -> dict | None:
    """Public GitHub org/repos lookup (no auth). Guesses org from the registrable domain label."""
    label = host.split(".")[-2] if host.count(".") >= 1 else host
    try:
        r = await client.get(f"https://api.github.com/orgs/{label}",
                             headers={"Accept": "application/vnd.github+json"})
        if r.status_code != 200:
            return None
        org = r.json()
        rr = await client.get(f"https://api.github.com/orgs/{label}/repos?per_page=15&sort=updated",
                              headers={"Accept": "application/vnd.github+json"})
        repos = []
        if rr.status_code == 200:
            for repo in rr.json():
                repos.append({"name": repo.get("name"), "language": repo.get("language"),
                              "stars": repo.get("stargazers_count"), "desc": (repo.get("description") or "")[:120]})
        return {"org": label, "public_repos": org.get("public_repos"), "repos": repos}
    except Exception:
        return None


async def _maybe_pricing(client: httpx.AsyncClient, base: str) -> dict | None:
    for path in ("/pricing", "/plans", "/pricing/"):
        try:
            r = await client.get(urllib.parse.urljoin(base, path))
            if r.status_code == 200 and len(r.text) > 500:
                from bs4 import BeautifulSoup
                text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
                tiers = re.findall(r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s?/\s?\w+)?", text)
                return {"url": str(r.url), "price_points": sorted(set(tiers))[:12], "excerpt": text[:800]}
        except Exception:
            continue
    return None


async def _graphql_introspection(client: httpx.AsyncClient, endpoints: list[str], base: str) -> dict | None:
    """ACTIVE, opt-in: a minimal introspection query against a discovered /graphql endpoint."""
    q = {"query": "{__schema{queryType{name} types{name kind}}}"}
    candidates = [e for e in endpoints if "graphql" in e.lower()] or ["/graphql"]
    for ep in candidates[:2]:
        url = ep if ep.startswith("http") else urllib.parse.urljoin(base, ep)
        try:
            r = await client.post(url, json=q, timeout=10)
            if r.status_code == 200 and "__schema" in r.text:
                data = r.json().get("data", {}).get("__schema", {})
                types = [t["name"] for t in data.get("types", []) if not t["name"].startswith("__")]
                return {"endpoint": url, "introspection": "OPEN", "type_count": len(types), "sample_types": types[:25]}
        except Exception:
            continue
    return None


async def _crawl_bundles(client: httpx.AsyncClient, assets: list[str], base: str, limit: int = 4) -> dict:
    """ACTIVE, opt-in: fetch a few of the app's own JS bundles and mine API paths the markup didn't show.
    These bundles are already public (the browser downloads them) — this is deeper endpoint discovery, not
    decompilation. Same-origin bundles first."""
    host = host_of(base)
    js = [a for a in assets if a.lower().split("?")[0].endswith(".js")]
    js.sort(key=lambda a: 0 if host_of(a) == host else 1)
    found: set[str] = set()
    crawled: list[str] = []
    for u in js[:limit]:
        try:
            r = await client.get(u, timeout=12)
            if r.status_code != 200:
                continue
            crawled.append(u.split("/")[-1][:44])
            for m in re.findall(r'["\'`](/(?:api|v1|v2|graphql|rest|rpc)[/\w.\-]*)', r.text):
                found.add(m)
            for m in re.findall(r'https?://[\w.\-]*api[\w.\-]*\.\w+[/\w.\-]*', r.text):
                found.add(m)
        except Exception:
            continue
    return {"bundles_crawled": crawled, "endpoints": sorted(found)[:40]}


async def analyze_web(target: str, active: bool = False) -> dict:
    from bs4 import BeautifulSoup
    url = normalize_url(target)
    host = host_of(url)
    result: dict = {"target": url, "host": host, "kind": "web", "notes": [], "phase2_pending": []}

    ok_pub, why = public_host_ok(host)
    if not ok_pub:
        result["error"] = why
        return result

    limits = httpx.Limits(max_connections=6)
    async with httpx.AsyncClient(headers={"User-Agent": BROWSER_UA}, follow_redirects=True,
                                 timeout=20.0, limits=limits) as client:
        ok, robots_note = await _robots_ok(client, url)
        result["robots"] = {"root_allowed": ok, "note": robots_note}
        try:
            resp = await client.get(url)
        except Exception as e:
            result["error"] = f"fetch failed: {type(e).__name__}: {e}"
            return result

        body = resp.text
        soup = BeautifulSoup(body, "html.parser")
        gen = None
        g = soup.find("meta", attrs={"name": "generator"})
        if g and g.get("content"):
            gen = g["content"]

        set_cookies = resp.headers.get_list("set-cookie") if hasattr(resp.headers, "get_list") else \
            ([resp.headers["set-cookie"]] if "set-cookie" in resp.headers else [])
        cookie_names = [c.split("=", 1)[0].strip() for c in set_cookies]
        assets = _extract_assets(body, url)

        result["fetched"] = {"final_url": str(resp.url), "status": resp.status_code,
                             "title": (soup.title.string.strip() if soup.title and soup.title.string else None),
                             "bytes": len(body)}
        result["headers"] = {h: resp.headers[h][:200] for h in INTERESTING_HEADERS if h in resp.headers}
        result["detections"] = signatures.match(dict(resp.headers), cookie_names, assets, body, gen)
        result["network"] = {**_dns_info(host), **_tls_info(host)}
        result["endpoints"] = _extract_endpoints(body)
        result["third_party_hosts"] = sorted({host_of(a) for a in assets if host_of(a) and host_of(a) != host})[:25]

        gh, pricing = await asyncio.gather(_github_org(client, host), _maybe_pricing(client, url))
        result["github"] = gh
        result["pricing"] = pricing

        if active:
            gql = await _graphql_introspection(client, result["endpoints"], url)
            result["active_graphql"] = gql or {"introspection": "closed/none-found"}
            bundles = await _crawl_bundles(client, assets, url)
            result["active_bundles"] = bundles
            result["endpoints"] = sorted(set(result["endpoints"]) | set(bundles["endpoints"]))[:60]
        else:
            result["phase2_pending"].append("deep-active endpoint/GraphQL/bundle probing (enable 'Deep recon')")

    result["phase2_pending"].append("job-listing stack mining (needs a search source)")
    return result


# ---- mobile (Phase-1 metadata stub; store scraping lands in Phase 2) ----
def looks_mobile(target: str) -> bool:
    t = target.lower()
    return ("play.google.com" in t or "apps.apple.com" in t or "itunes.apple.com" in t
            or t.startswith("app:") or "/store/apps/" in t)


async def analyze_mobile(target: str) -> dict:
    """Public app-store listing teardown: metadata + framework/SDK hints from the store page.
    (App internals live in the binary — out of scope by our red lines — so build details are INFERRED.)"""
    from bs4 import BeautifulSoup
    t = normalize_url(target)
    store = ("Google Play" if "play.google.com" in t else
             "App Store" if ("apps.apple.com" in t or "itunes.apple.com" in t) else "unknown")
    result: dict = {"target": t, "host": host_of(t), "kind": "mobile", "store": store, "notes": [], "phase2_pending": []}
    ok_pub, why = public_host_ok(host_of(t))
    if not ok_pub:
        result["error"] = why
        return result
    async with httpx.AsyncClient(headers={"User-Agent": BROWSER_UA}, follow_redirects=True, timeout=20.0) as client:
        ok, robots_note = await _robots_ok(client, t)
        result["robots"] = {"root_allowed": ok, "note": robots_note}
        try:
            r = await client.get(t)
        except Exception as e:
            result["error"] = f"fetch failed: {type(e).__name__}: {e}"
            return result
        html = r.text
        soup = BeautifulSoup(html, "html.parser")

        def meta(name=None, prop=None):
            tag = soup.find("meta", attrs={"name": name}) if name else soup.find("meta", attrs={"property": prop})
            return tag.get("content").strip() if tag and tag.get("content") else None

        title = meta(prop="og:title") or (soup.title.string.strip() if soup.title and soup.title.string else None)
        desc = meta(name="description") or meta(prop="og:description")
        # framework / SDK hints occasionally leaked in the listing (App Store often names the seller/tech)
        hints = []
        for name, pat in (("React Native", r"react[- ]?native"), ("Flutter", r"flutter"),
                          ("Unity", r"unity"), ("Firebase", r"firebase"), ("Ionic/Capacitor", r"ionic|capacitor")):
            if re.search(pat, html, re.I):
                hints.append(name)
        result["fetched"] = {"final_url": str(r.url), "status": r.status_code, "title": title, "bytes": len(html)}
        result["store_meta"] = {"title": title, "description": (desc or "")[:600],
                                "genre": meta(prop="og:type"), "image": meta(prop="og:image")}
        result["framework_hints"] = hints
        result["detections"] = [{"name": h, "category": "mobile-framework", "confidence": "low",
                                 "evidence": "mentioned in store listing"} for h in hints]
        result["notes"].append("Mobile teardown = PUBLIC store-listing metadata + any framework hints on the page. "
                               "The app binary is out of scope (no decompilation), so stack/architecture is INFERRED "
                               "from the app category and public signals — treat confidence as low unless hints appear.")
        result["phase2_pending"].append("APK/IPA-level SDK & permission analysis is intentionally NOT done (red line)")
    return result


async def collect(target: str, active: bool = False) -> dict:
    if looks_mobile(target):
        return await analyze_mobile(target)
    return await analyze_web(target, active=active)
