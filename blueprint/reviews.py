"""Voice-of-Customer collector — pull PUBLIC app-store reviews so BLUEPRINT can mine the complaints users
actually have about a product and turn them into 'beat-it-here' differentiators for a rebuild.

Inspired by the "entrepreneur skill mines the original's reviews" idea (@bengusberg 'Replica' TikTok, 2026-10-14),
built inside BLUEPRINT's RED LINES: reviews are PUBLIC signals (Apple's keyless RSS JSON; Google Play best-effort),
nothing behind a login, no IP lift — we read what any shopper sees and learn what to do better.

API:
    payload = await collect_reviews("https://apps.apple.com/us/app/notion/id1232780281")   # or a bare name
    # -> {ok, query, sources, reviews:[{source,rating,title,text,version,author,date}], low_star:[...], note}
"""
from __future__ import annotations

import re

import httpx

try:
    from blueprint.collectors import BROWSER_UA
except Exception:  # pragma: no cover
    BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

_APPLE_ID_RE = re.compile(r"/id(\d{5,})")
_PLAY_PKG_RE = re.compile(r"[?&]id=([A-Za-z0-9._]+)")
_HDRS = {"User-Agent": BROWSER_UA, "Accept": "application/json"}


# ───────────────────────── Apple App Store (keyless RSS) ─────────────────────────
async def _resolve_apple(client: httpx.AsyncClient, query: str, country: str) -> dict | None:
    """From an App Store URL or a plain name → {id, name, url}. URL id wins; otherwise iTunes Search."""
    m = _APPLE_ID_RE.search(query or "")
    if m:
        app_id = m.group(1)
        try:
            r = await client.get(f"https://itunes.apple.com/lookup?id={app_id}&country={country}",
                                  headers=_HDRS, timeout=12)
            res = (r.json().get("results") or [{}])[0]
            return {"id": app_id, "name": res.get("trackName", f"app {app_id}"),
                    "url": res.get("trackViewUrl", "")}
        except Exception:
            return {"id": app_id, "name": f"app {app_id}", "url": ""}
    term = re.sub(r"https?://\S+", "", query or "").strip() or query
    try:
        r = await client.get("https://itunes.apple.com/search", headers=_HDRS, timeout=12,
                              params={"term": term, "entity": "software", "limit": 1, "country": country})
        res = (r.json().get("results") or [])
        if res:
            return {"id": str(res[0]["trackId"]), "name": res[0]["trackName"],
                    "url": res[0].get("trackViewUrl", "")}
    except Exception:
        pass
    return None


async def _apple_reviews(client: httpx.AsyncClient, app_id: str, country: str, pages: int) -> list:
    out = []
    for n in range(1, max(1, pages) + 1):
        url = (f"https://itunes.apple.com/{country}/rss/customerreviews/"
               f"page={n}/id={app_id}/sortby=mostrecent/json")
        try:
            r = await client.get(url, headers=_HDRS, timeout=14)
            entries = (r.json().get("feed", {}) or {}).get("entry", []) or []
        except Exception:
            break
        got = 0
        for e in entries:
            if "im:rating" not in e:                 # the first entry is the app itself, not a review
                continue
            try:
                out.append({
                    "source": "apple",
                    "rating": int(e["im:rating"]["label"]),
                    "title": (e.get("title", {}) or {}).get("label", "").strip(),
                    "text": (e.get("content", {}) or {}).get("label", "").strip(),
                    "version": (e.get("im:version", {}) or {}).get("label", ""),
                    "author": (((e.get("author") or {}).get("name") or {}).get("label", "")),
                    "date": (e.get("updated", {}) or {}).get("label", "")[:10],
                })
                got += 1
            except Exception:
                continue
        if got == 0:
            break
    return out


# ───────────────────────── Google Play (best-effort; optional dep) ─────────────────────────
async def _play_reviews(query: str, country: str, count: int) -> tuple[list, str]:
    """Play has no clean public reviews API. Use google-play-scraper if installed; else skip gracefully."""
    try:
        from google_play_scraper import reviews as gp_reviews, search as gp_search, Sort
    except Exception:
        return [], "play-skipped (google-play-scraper not installed)"
    m = _PLAY_PKG_RE.search(query or "")
    pkg = m.group(1) if m else None
    try:
        if not pkg:
            hits = gp_search(re.sub(r"https?://\S+", "", query or "").strip() or query, n_hits=1, country=country)
            if not hits:
                return [], "play-no-match"
            pkg = hits[0]["appId"]
        res, _ = gp_reviews(pkg, count=count, country=country, sort=Sort.NEWEST)
        out = [{"source": "play", "rating": int(x.get("score") or 0), "title": "",
                "text": (x.get("content") or "").strip(), "version": x.get("reviewCreatedVersion") or "",
                "author": x.get("userName") or "", "date": str(x.get("at"))[:10]} for x in res]
        return out, f"play ok ({pkg})"
    except Exception as e:  # noqa: BLE001
        return [], f"play-error ({type(e).__name__})"


# ───────────────────────── Reddit (public JSON) ─────────────────────────
async def _reddit_reviews(client: httpx.AsyncClient, query: str, limit: int) -> tuple[list, str]:
    """Candid user complaints/opinions from Reddit's public search JSON — great 'X alternative / X sucks' signal."""
    term = re.sub(r"https?://\S+", "", query or "").strip() or query
    try:
        r = await client.get("https://www.reddit.com/search.json",
                              headers={"User-Agent": "blueprint-voc/1.0 (public review research)"}, timeout=12,
                              params={"q": f"{term} review", "limit": limit, "sort": "relevance", "t": "year"})
        kids = (r.json().get("data", {}) or {}).get("children", []) or []
    except Exception as e:  # noqa: BLE001
        return [], f"reddit-error ({type(e).__name__})"
    out = []
    for k in kids:
        d = k.get("data", {}) or {}
        body = (d.get("selftext") or "").strip()
        title = (d.get("title") or "").strip()
        text = (title + ". " + body).strip(". ").strip()
        if len(text) < 20:
            continue
        out.append({"source": "reddit", "rating": None, "title": title[:120], "text": text[:600],
                    "version": "", "author": "r/" + (d.get("subreddit") or ""),
                    "date": "", "url": "https://reddit.com" + (d.get("permalink") or "")})
    return out, f"reddit ok ({len(out)})" if out else "reddit-none"


# ───────────────────────── Open web (any review site via search) ─────────────────────────
_REVIEW_HINT_DOMAINS = ("trustpilot", "g2.com", "capterra", "sitejabber", "producthunt", "getapp",
                        "softwareadvice", "trustradius", "reddit.com", "apps.apple", "play.google",
                        "glassdoor", "consumeraffairs", "reviews.io", "yelp")


def _ddgs_search(term: str, n: int) -> list:
    """Sync DuckDuckGo search (run in a thread). Returns [{title, href, body}]. Any review source on the web."""
    try:
        try:
            from ddgs import DDGS
        except Exception:
            from duckduckgo_search import DDGS
    except Exception:
        return []
    try:
        with DDGS() as d:
            return list(d.text(f'{term} reviews complaints problems', max_results=n)) or []
    except Exception:
        return []


async def _web_reviews(query: str, limit: int) -> tuple[list, str]:
    """Hybrid 'any source': web-search the product's reviews and use the result snippets as mini-reviews.
    Covers G2/Trustpilot/Capterra/forums/blogs — anything indexed — so it's not limited to the app stores."""
    import asyncio
    term = re.sub(r"https?://\S+", "", query or "").strip() or query
    results = await asyncio.to_thread(_ddgs_search, term, limit)
    if not results:
        return [], "web-skipped (no search results / ddgs unavailable)"
    out = []
    for r in results:
        body = (r.get("body") or "").strip()
        if len(body) < 30:
            continue
        href = r.get("href") or r.get("url") or ""
        dom = re.sub(r"^www\.", "", (re.findall(r"https?://([^/]+)", href) or [""])[0])
        out.append({"source": "web", "rating": None, "title": (r.get("title") or "")[:140],
                    "text": body[:600], "version": "", "author": dom, "date": "", "url": href})
    return out, f"web ok ({len(out)})"


# ───────────────────────── public entrypoint ─────────────────────────
async def collect_reviews(query: str, country: str = "us", pages: int = 4, play_count: int = 120,
                          sources_on=("apple", "play", "web", "reddit")) -> dict:
    """HYBRID: gather public reviews from the app stores AND the open web (G2/Trustpilot/Reddit/etc.), merge,
    and return them for complaint-mining. Each source is best-effort — one failing never sinks the rest."""
    query = (query or "").strip()
    if not query:
        return {"ok": False, "error": "no product name or URL given", "reviews": []}
    sources, reviews, notes = {}, [], []
    async with httpx.AsyncClient(follow_redirects=True) as client:
        if "apple" in sources_on:
            app = await _resolve_apple(client, query, country)
            if app:
                ar = await _apple_reviews(client, app["id"], country, pages)
                sources["apple"] = {**app, "count": len(ar)}
                reviews += ar
                notes.append("apple-no-reviews" if not ar else f"apple ok ({len(ar)})")
            else:
                notes.append("apple-no-match")
        if "play" in sources_on:
            pr, pnote = await _play_reviews(query, country, play_count)
            sources["play"] = {"count": len(pr)}
            reviews += pr
            notes.append(pnote)
        if "reddit" in sources_on:
            rr, rnote = await _reddit_reviews(client, query, 25)
            sources["reddit"] = {"count": len(rr)}
            reviews += rr
            notes.append(rnote)
        if "web" in sources_on:
            wr, wnote = await _web_reviews(query, 18)
            sources["web"] = {"count": len(wr)}
            reviews += wr
            notes.append(wnote)

    low_star = [r for r in reviews if r.get("rating") and r["rating"] <= 2]
    return {
        "ok": bool(reviews),
        "query": query,
        "country": country,
        "sources": sources,
        "counts": {"total": len(reviews), "low_star": len(low_star),
                   "apple": sources.get("apple", {}).get("count", 0),
                   "play": sources.get("play", {}).get("count", 0),
                   "web": sources.get("web", {}).get("count", 0),
                   "reddit": sources.get("reddit", {}).get("count", 0)},
        "reviews": reviews,
        "low_star": low_star,
        "note": "; ".join(n for n in notes if n),
    }
