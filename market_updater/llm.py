"""Model access: Claude (on the Max plan's API credit) with Groq's free tier as fallback.

Every call is logged with its token use and estimated cost, and a credit guard moves
cheap jobs to Groq once month-to-date Claude spend passes the configured threshold.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable

from .db import DB
from .settings import Settings
from .util import http, log

# USD per million tokens (input, output). Haiku's price steps up above 100K-token prompts.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-5-5@long": (0.50, 2.50),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
}
GROQ_CHAT_MODEL = os.environ.get("GROQ_CHAT_MODEL", "openai/gpt-oss-120b")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


class LLMUnavailable(RuntimeError):
    """Raised when neither provider can serve a call (the caller degrades gracefully)."""


def _cost(model: str, inp: int, out: int) -> float:
    key = model
    if model == "claude-haiku-5-5" and inp > 100_000:
        key = "claude-haiku-5-5@long"
    pin, pout = PRICES.get(key, (0.0, 0.0))
    return inp / 1e6 * pin + out / 1e6 * pout


def _extract_json(text: str) -> Any:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=0)
    return json.loads(text[start:])


class LLM:
    def __init__(self, settings: Settings, db: DB):
        self.settings = settings
        self.db = db
        self._anthropic = None
        if os.environ.get("ANTHROPIC_API_KEY"):
            import anthropic
            self._anthropic = anthropic.Anthropic(max_retries=3)
        self.groq_key = os.environ.get("GROQ_API_KEY")

    # -- routing ---------------------------------------------------------------
    def _model_for(self, tier: str) -> tuple[str, str]:
        cfg = self.settings.get(f"models.{tier}", {}) or {}
        return cfg.get("model", "claude-sonnet-5-5"), cfg.get("fallback", "groq")

    def _claude_allowed(self, tier: str) -> bool:
        if not self._anthropic:
            return False
        guard = self.settings.get("models.credit_guard", {}) or {}
        spent = self.db.month_spend("anthropic")
        if spent >= float(guard.get("monthly_credit_usd", 100)) * 0.98:
            log.warning("Claude credit nearly used ($%.2f); using fallback for %s", spent, tier)
            return False
        if tier == "extract" and spent >= float(guard.get("move_extraction_to_fallback_at_usd", 80)):
            return False
        return True

    # -- public API ------------------------------------------------------------
    def json(self, *, tier: str, purpose: str, system: str, user: str, schema: dict,
             max_tokens: int = 4000) -> dict:
        """Structured output. Claude is forced to call a tool whose input is the schema."""
        model, fallback = self._model_for(tier)
        if self._claude_allowed(tier):
            try:
                return self._claude_json(model, purpose, system, user, schema, max_tokens)
            except Exception as e:
                log.warning("Claude call failed for %s (%s); trying fallback", purpose, e)
        if fallback == "groq" and self.groq_key:
            return self._groq_json(purpose, system, user, schema, max_tokens)
        raise LLMUnavailable(f"No model available for {purpose}")

    def text(self, *, tier: str, purpose: str, system: str, user: str, max_tokens: int = 3000) -> str:
        model, fallback = self._model_for(tier)
        if self._claude_allowed(tier):
            try:
                return self._claude_text(model, purpose, system, user, max_tokens)
            except Exception as e:
                log.warning("Claude call failed for %s (%s); trying fallback", purpose, e)
        if fallback == "groq" and self.groq_key:
            return self._groq_text(purpose, system, user, max_tokens)
        raise LLMUnavailable(f"No model available for {purpose}")

    # -- Claude ----------------------------------------------------------------
    def _claude_json(self, model, purpose, system, user, schema, max_tokens) -> dict:
        tool = {"name": "submit", "description": "Submit the structured result.", "input_schema": schema}
        resp = self._anthropic.messages.create(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
            tools=[tool], tool_choice={"type": "tool", "name": "submit"},
        )
        self._log_claude(model, resp, purpose)
        for block in resp.content:
            if getattr(block, "type", "") == "tool_use":
                return dict(block.input)
        raise ValueError("Claude returned no tool call")

    def _claude_text(self, model, purpose, system, user, max_tokens) -> str:
        resp = self._anthropic.messages.create(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
        self._log_claude(model, resp, purpose)
        return "".join(getattr(b, "text", "") for b in resp.content)

    def _log_claude(self, model, resp, purpose) -> None:
        u = resp.usage
        inp = (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0)
        out = u.output_tokens or 0
        self.db.log_usage("anthropic", model, inp, out, _cost(model, inp, out), purpose)

    # -- Groq ------------------------------------------------------------------
    def _groq_post(self, payload: dict, purpose: str) -> dict:
        for attempt in range(4):
            r = http().post(GROQ_URL, json=payload,
                            headers={"Authorization": f"Bearer {self.groq_key}"}, timeout=120)
            if r.status_code == 429:
                wait = float(r.headers.get("retry-after", 10 * (attempt + 1)))
                log.info("Groq rate limit; waiting %.0fs", wait)
                time.sleep(min(wait, 60))
                continue
            r.raise_for_status()
            data = r.json()
            u = data.get("usage", {})
            self.db.log_usage("groq", payload["model"], u.get("prompt_tokens", 0),
                              u.get("completion_tokens", 0), 0.0, purpose)
            return data
        raise LLMUnavailable("Groq rate-limited")

    def _groq_json(self, purpose, system, user, schema, max_tokens) -> dict:
        sys = (system + "\n\nReturn only a JSON object that matches this JSON schema:\n"
               + json.dumps(schema))
        data = self._groq_post({
            "model": GROQ_CHAT_MODEL, "max_tokens": max_tokens, "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": sys}, {"role": "user", "content": user}],
        }, purpose)
        return _extract_json(data["choices"][0]["message"]["content"])

    def _groq_text(self, purpose, system, user, max_tokens) -> str:
        data = self._groq_post({
            "model": GROQ_CHAT_MODEL, "max_tokens": max_tokens, "temperature": 0.3,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }, purpose)
        return data["choices"][0]["message"]["content"]


class FakeLLM:
    """Test double: answers from a callback keyed on purpose."""

    def __init__(self, responder: Callable[[str, str, str], Any]):
        self.responder = responder
        self.calls: list[tuple[str, str]] = []

    def json(self, *, tier, purpose, system, user, schema, max_tokens=4000):
        self.calls.append((purpose, user))
        return self.responder(purpose, system, user)

    def text(self, *, tier, purpose, system, user, max_tokens=3000):
        self.calls.append((purpose, user))
        return self.responder(purpose, system, user)
