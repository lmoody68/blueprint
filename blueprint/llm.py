"""Groq LLM client for BLUEPRINT.

Key is read at runtime from Documents/API_Keys.txt (first gsk_ token) — never hardcoded.
Groq's edge (Cloudflare) rejects non-browser/non-curl User-Agents with error 1010, so we send
a curl UA on the API call only. Target websites are fetched with a normal browser UA (collectors.py).
"""
from __future__ import annotations
import os
import re
import json
import asyncio
import httpx

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = os.getenv("BLUEPRINT_MODEL", "openai/gpt-oss-120b")
_KEYS_FILE = os.getenv("API_KEYS_FILE", r"C:\Users\lesli\Documents\API_Keys.txt")

_KEY_CACHE: str | None = None


def get_groq_key() -> str:
    """Return the first Groq (gsk_) key found in the keys file. Cached in-process."""
    global _KEY_CACHE
    if _KEY_CACHE:
        return _KEY_CACHE
    env = os.getenv("GROQ_API_KEY")
    if env and env.startswith("gsk_"):
        _KEY_CACHE = env.strip()
        return _KEY_CACHE
    try:
        with open(_KEYS_FILE, "r", encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
    except OSError as e:
        raise RuntimeError(f"Cannot read keys file {_KEYS_FILE}: {e}")
    m = re.search(r"gsk_[A-Za-z0-9]{20,}", text)
    if not m:
        raise RuntimeError("No Groq (gsk_) key found in keys file.")
    _KEY_CACHE = m.group(0)
    return _KEY_CACHE


_RETRYABLE = {400, 429, 500, 502, 503, 520, 524}   # 400 included: Groq's gpt-oss intermittently 400s on a
                                                    # valid payload that then succeeds on retry (observed).


async def chat(messages: list[dict], *, model: str | None = None, temperature: float = 0.3,
               max_tokens: int = 5000, json_mode: bool = False, timeout: float = 90.0,
               retries: int = 4) -> str:
    """POST a chat completion to Groq and return the assistant text.

    Retries with backoff on transient failures (429 rate-limit, 5xx, Cloudflare 520/524, network
    blips) — Groq's free tier throttles under bursts, so a hard fail here would drop a whole teardown."""
    payload: dict = {
        "model": model or DEFAULT_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {
        "Authorization": f"Bearer {get_groq_key()}",
        "Content-Type": "application/json",
        "User-Agent": "curl/8.5.0",  # avoids Cloudflare 1010 on api.groq.com
    }
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(retries):
            try:
                r = await client.post(GROQ_URL, headers=headers, json=payload)
                if r.status_code in _RETRYABLE and attempt < retries - 1:
                    retry_after = float(r.headers.get("retry-after", 0) or 0)
                    await asyncio.sleep(retry_after or (1.2 * (attempt + 1)))
                    continue
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
            except httpx.HTTPStatusError as e:
                last_exc = e
                if e.response.status_code in _RETRYABLE and attempt < retries - 1:
                    await asyncio.sleep(1.2 * (attempt + 1))
                    continue
                raise
            except httpx.TransportError as e:
                last_exc = e
                if attempt < retries - 1:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                raise
    if last_exc:
        raise last_exc
    raise RuntimeError("Groq chat: exhausted retries with no response")


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of an LLM reply, tolerant of prose/code fences."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    start = None
    raise ValueError("No parseable JSON object in LLM reply.")
