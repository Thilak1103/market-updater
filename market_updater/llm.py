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


_DROP = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength",
         "minItems", "maxItems", "pattern"}


def strict_schema(schema: Any) -> Any:
    """Copy of a JSON schema fit for structured outputs: objects closed, unsupported limits removed."""
    if isinstance(schema, dict):
        out = {k: strict_schema(v) for k, v in schema.items() if k not in _DROP}
        if out.get("type") == "object":
            out.setdefault("additionalProperties", False)
        return out
    if isinstance(schema, list):
        return [strict_schema(v) for v in schema]
    return schema


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
            try:
                return self._groq_json(purpose, system, user, schema, max_tokens)
            except LLMUnavailable:
                raise
            except Exception as e:
                raise LLMUnavailable(f"Fallback failed for {purpose}: {e}") from e
        raise LLMUnavailable(f"No model available for {purpose}")

    def text(self, *, tier: str, purpose: str, system: str, user: str, max_tokens: int = 3000) -> str:
        model, fallback = self._model_for(tier)
        if self._claude_allowed(tier):
            try:
                return self._claude_text(model, purpose, system, user, max_tokens)
            except Exception as e:
                log.warning("Claude call failed for %s (%s); trying fallback", purpose, e)
        if fallback == "groq" and self.groq_key:
            try:
                return self._groq_text(purpose, system, user, max_tokens)
            except LLMUnavailable:
                raise
            except Exception as e:
                raise LLMUnavailable(f"Fallback failed for {purpose}: {e}") from e
        raise LLMUnavailable(f"No model available for {purpose}")

    # -- Claude ----------------------------------------------------------------
    def _budget(self, max_tokens: int) -> int:
        """Claude 5.5 models always think, and thinking counts toward max_tokens. Leave room for it."""
        return min(max(max_tokens * 4, 16_000), 64_000)

    def _send(self, kwargs: dict):
        """Stream the call: the SDK refuses non-streamed calls with large max_tokens (over ~21K)."""
        stream = getattr(self._anthropic.messages, "stream", None)
        if stream is None:                       # test doubles
            return self._anthropic.messages.create(**kwargs)
        with stream(**kwargs) as s:
            return s.get_final_message()

    def _create(self, kwargs: dict, purpose: str):
        """One call, retried once with double the budget if the answer was cut off."""
        resp = self._send(kwargs)
        self._log_claude(kwargs["model"], resp, purpose)
        if getattr(resp, "stop_reason", None) == "max_tokens" and kwargs["max_tokens"] < 64_000:
            log.warning("%s ran out of room at %d tokens; retrying with more", purpose, kwargs["max_tokens"])
            kwargs = {**kwargs, "max_tokens": min(kwargs["max_tokens"] * 2, 64_000)}
            resp = self._send(kwargs)
            self._log_claude(kwargs["model"], resp, purpose)
        if getattr(resp, "stop_reason", None) == "max_tokens":
            raise ValueError(f"{purpose}: answer cut off at {kwargs['max_tokens']} tokens")
        return resp

    def _claude_json(self, model, purpose, system, user, schema, max_tokens) -> dict:
        """Structured outputs (output_config.format). Opus 5.5 and Sonnet 5.5 reject forced tool use
        with a 400, so the old forced-tool route left every answer to a loose 'auto' mode where
        thinking ate the budget and answers came back cut off or empty. Tool use stays as a fallback."""
        base = dict(model=model, max_tokens=self._budget(max_tokens), system=system,
                    messages=[{"role": "user", "content": user}])
        no_structured = getattr(self, "_no_structured", set())
        self._no_structured = no_structured
        if model not in no_structured:
            try:
                resp = self._create({**base, "output_config": {"format": {
                    "type": "json_schema", "schema": strict_schema(schema)}}}, purpose)
                text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
                return json.loads(text)
            except Exception as e:
                if "output_config" not in str(e) and "json_schema" not in str(e) and "schema" not in str(e):
                    raise
                log.warning("Structured output refused for %s (%s); using tool use", model, e)
                no_structured.add(model)
        tool = {"name": "submit", "description": "Submit the structured result.", "input_schema": schema}
        kwargs = {**base, "tools": [tool],
                  "system": system + "\n\nRespond only by calling the `submit` tool once."}
        no_force = getattr(self, "_no_forced_tool", set())
        self._no_forced_tool = no_force
        resp = None
        if model not in no_force:
            try:
                resp = self._create({**kwargs, "tool_choice": {"type": "tool", "name": "submit"}}, purpose)
            except Exception as e:
                if "tool" not in str(e):
                    raise
                no_force.add(model)
        if resp is None:
            resp = self._create({**kwargs, "tool_choice": {"type": "auto"}}, purpose)
        for block in resp.content:
            if getattr(block, "type", "") == "tool_use":
                return dict(block.input)
        text = "".join(getattr(b, "text", "") for b in resp.content)
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise ValueError("Claude returned no tool call")

    def _claude_text(self, model, purpose, system, user, max_tokens) -> str:
        resp = self._send(dict(model=model, max_tokens=self._budget(max_tokens), system=system,
                               messages=[{"role": "user", "content": user}]))
        self._log_claude(model, resp, purpose)
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "text") == "text")
        if getattr(resp, "stop_reason", None) == "max_tokens":
            from .analysis.checks import trim_incomplete
            log.warning("%s hit its token limit; trimming the unfinished sentence", purpose)
            text = trim_incomplete(text)
        return text

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
        choice = data["choices"][0]
        text = choice["message"]["content"]
        if choice.get("finish_reason") == "length":
            from .analysis.checks import trim_incomplete
            text = trim_incomplete(text)
        return text


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
