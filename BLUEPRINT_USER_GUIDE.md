# BLUEPRINT — User Guide

**Reverse-engineer any app from public signals into a build-and-beat playbook.**
Live: **https://blueprint.mac-vision.com** · Source: **github.com/lmoody68/blueprint**

BLUEPRINT points at any product (a website, SaaS, or app) and infers **how it was likely built** —
purely from signals a browser can already see — then hands you a step-by-step plan to build a better
version. Every claim is backed by the exact evidence that proved it.

---

## 1. The 30-second version
1. Open **https://blueprint.mac-vision.com**.
2. Type a site — e.g. `linear.app`, `notion.so`, `stripe.com` (a domain, full URL, or an app-store link).
3. *(Optional)* tick **Deep recon** for a more thorough scan (probes API endpoints + JavaScript bundles).
4. Click **Analyze**. In ~10-15 seconds you get a full teardown across nine tabs.

That's it. Everything below explains what you're looking at.

---

## 2. What each tab shows
- **Overview** — a plain-English summary, two scores, and the business model.
  - **Composite score (0-100):** how well-understood *and* clonable the app is.
  - **Evidence score (0-100):** how much *hard* public evidence BLUEPRINT actually gathered — it shows
    its work (`+14 public GitHub`, `+8 pricing`, …). A confident read on thin evidence will say so.
  - **Business model:** SaaS / B2B / B2C / marketplace… inferred from pricing and sign-in signals.
  - **Comparable products:** 2-3 real competitors so you see the landscape.
- **Stack** — the inferred frontend / backend / database / infra / third-party technologies, each tagged
  **observed** (seen in the signals) or **inferred** (a reasonable guess), with a confidence level.
- **Architecture** — how the pieces fit together, plus a diagram.
- **Build Playbook** — the ordered steps to recreate a comparable app.
- **Improvements** — ranked ways to build it *better* (each tagged impact + effort).
- **MVP Clone** — a lean first-version spec: pitch, core features, **build difficulty (1-10)**, **market potential ($–$$$)**, milestones.
- **Clone Studio** ⭐ — a concrete, buildable brief: recommended stack, a real **project file tree**, key
  files, setup commands, and environment variables. Two buttons:
  - **⬇ Download build brief (.md)** — save the brief.
  - **🛠 Copy build prompt** — copies a ready-to-paste prompt for an AI app builder (**Emergent, Lovable,
    Bolt, or Claude**) so you can actually *build the clone* from the teardown.
- **Ask BLUEPRINT** 💬 — chat with the teardown. Ask *"How would I build their auth?"*, *"What's the likely
  DB schema?"*, *"What's their biggest weakness?"* — answers are grounded in the gathered evidence.
- **Evidence** — the receipts: every detected technology with the exact header/cookie/asset that proved it,
  endpoints found, public GitHub repos, pricing, and response headers.

**Export anytime:** the **⬇ MD** and **⬇ JSON** buttons (top-right of the tabs) download the whole teardown.

---

## 3. Deep recon (the checkbox)
Leave it off for a fast passive scan. Tick it to also: probe for an open **GraphQL** schema and **crawl the
app's JavaScript bundles** to surface API endpoints the homepage didn't reveal. Slightly slower, more thorough.

## 4. Mobile apps
Paste a **Google Play** or **App Store** link and BLUEPRINT reads the public listing (name, description,
framework hints) and infers the likely build. *(App internals live in the binary, which BLUEPRINT never opens
— so mobile confidence is lower unless the listing leaks framework hints.)*

---

## 5. What BLUEPRINT will NOT do (by design)
It works from **public signals only**. It honors `robots.txt`, keeps deep-recon opt-in, and enforces hard
red lines in code: **no dark web, no decompiling apps, no bypassing DRM, no scraping behind logins, and it
never copies proprietary code** — it *infers* an architecture, it doesn't lift anyone's IP. That's what makes
it safe to demo and to build on.

## 6. Tips
- Marketing homepages score higher than bare app shells (more public signals to read).
- Use **Ask BLUEPRINT** to go deeper on any one piece before you start building.
- Use **Clone Studio → Copy build prompt** to jump straight from "how it's built" to actually building it.
- If you hit a rate-limit message, wait a minute — it protects the shared AI key.

## 7. Troubleshooting
- **"blocked (SSRF guard)"** — you aimed it at a private/internal address; that's intentionally refused.
- **"Daily cap reached"** — the shared analysis budget resets tomorrow.
- **Diagram didn't draw** — the rest of the report is unaffected; reload and re-run.
