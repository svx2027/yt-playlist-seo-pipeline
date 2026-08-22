"""
_gemini.py: shared Gemini REST helper for the pipeline (Phases 4 and 6).

Matches the same lightweight REST-call pattern used elsewhere in this pipeline family:
REST call to generativelanguage.googleapis.com, key loaded from a .env by path
via python-dotenv. No heavy SDK.

SECURITY (non-negotiable, see repo CLAUDE.md safety rails):
  - The API key is loaded from config['gemini_env_path'] and NEVER printed,
    logged, or returned. Callers get text, not the key.
  - Transcripts and descriptions are UNTRUSTED text. Callers must wrap them in
    delimiters and instruct the model to treat them as data. This module does
    not itself sanitise content; it just transports prompts.
"""

import json
import os
import time

import requests
from dotenv import load_dotenv

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"


def load_key(cfg):
    """Load GEMINI_API_KEY from the .env named by config['gemini_env_path'].

    Returns the key string. Never print or log the return value.
    """
    env_path = cfg.get("gemini_env_path", "")
    if not env_path or not os.path.exists(env_path):
        raise SystemExit(f"ERROR: gemini_env_path missing or not found: {env_path!r}")
    load_dotenv(env_path)
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise SystemExit("ERROR: GEMINI_API_KEY not set in that .env file.")
    return key


def generate(key, model, prompt, *, json_mode=False, schema=None,
             temperature=0.4, max_output_tokens=None, thinking_budget=None,
             timeout=120, max_retries=4):
    """One generateContent call. Returns (text, usage_dict).

    json_mode=True asks Gemini for application/json; if `schema` is given it is
    passed as responseSchema so the model is constrained to that shape.
    thinking_budget=0 turns OFF the 2.5-flash "thinking" tokens (they otherwise
    eat the output budget and can truncate structured output).
    Retries 429/500/502/503/504 with exponential backoff. Raises on hard failure,
    including a truncated (MAX_TOKENS) response, so callers never parse half a JSON.
    """
    gen_cfg = {"temperature": temperature}
    if json_mode:
        gen_cfg["response_mime_type"] = "application/json"
        if schema:
            gen_cfg["response_schema"] = schema
    if max_output_tokens:
        gen_cfg["max_output_tokens"] = max_output_tokens
    if thinking_budget is not None:
        gen_cfg["thinkingConfig"] = {"thinkingBudget": thinking_budget}

    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen_cfg}
    url = f"{API_ROOT}/{model}:generateContent"

    last_err = None
    for attempt in range(max_retries):
        try:
            r = requests.post(url, params={"key": key}, json=body, timeout=timeout)
        except requests.RequestException as e:
            last_err = f"network error: {e}"
            time.sleep(2 ** attempt + 0.5)
            continue

        if r.status_code == 200:
            j = r.json()
            cands = j.get("candidates", [])
            if not cands:
                raise RuntimeError(f"Gemini returned no candidates: {json.dumps(j)[:300]}")
            finish = cands[0].get("finishReason", "")
            parts = cands[0].get("content", {}).get("parts", [{}])
            text = "".join(p.get("text", "") for p in parts).strip()
            if not text:
                raise RuntimeError(f"Gemini returned empty text (finishReason={finish}).")
            if finish == "MAX_TOKENS":
                raise RuntimeError("Gemini response hit MAX_TOKENS (truncated). "
                                   "Raise max_output_tokens or set thinking_budget=0.")
            return text, j.get("usageMetadata", {})

        # Retry only on transient statuses.
        if r.status_code in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
            wait = 2 ** attempt + 0.5
            time.sleep(wait)
            last_err = f"HTTP {r.status_code}"
            continue

        # Hard failure: surface status + a trimmed body (never includes the key).
        raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:300]}")

    raise RuntimeError(f"Gemini failed after {max_retries} attempts. Last: {last_err}")


def parse_json(text):
    """Parse model JSON, tolerating ```json fences if json_mode was not honoured."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.startswith("json"):
            t = t[4:]
        t = t.strip().rstrip("`").strip()
    return json.loads(t)
