"""
groq_client.py — thin wrapper around Groq's OpenAI-compatible chat
completions endpoint.

llama-3.3-70b-versatile (the long-standing Groq default) was deprecated
for free/dev-tier usage in June 2026; openai/gpt-oss-120b is Groq's
current recommended general-purpose replacement. Override via GROQ_MODEL
in .env if that changes again.
"""

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

_SEARCH_DIRS = [Path(__file__).resolve().parent] + list(Path(__file__).resolve().parents)

for _d in _SEARCH_DIRS:
    _candidate = _d / ".env"
    if _candidate.is_file():
        load_dotenv(_candidate)
        break
else:
    load_dotenv()

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")


class GroqError(RuntimeError):
    """Raised on any failure to get a usable response from Groq."""


def call_groq(prompt: str, model: str = DEFAULT_MODEL, timeout: int = 30) -> str:
    """Send `prompt` as a single user message to Groq's chat completions API, return the reply text."""
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise GroqError("GROQ_API_KEY not set -- check your .env file.")

    try:
        resp = requests.post(
            GROQ_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise GroqError(f"Groq API call failed: {e}") from e

    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise GroqError(f"Unexpected Groq response shape: {data}") from e
