"""AI provider adapter. The engine only ever sees `Provider.complete()`, so tests swap in FakeProvider.

- HeuristicProvider: offline keyword classifier, default for demos (no API key needed). It can also play a
  "fooled" model for the prompt-injection demo: if the customer text tries to inject instructions it first
  returns an out-of-schema answer, which schema enforcement rejects; on the validation-error retry it
  returns a clean answer.
- AnthropicProvider / OpenAICompatProvider: real models over HTTPS with a timeout.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from . import httpclient


@dataclass
class ModelResponse:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ProviderError(Exception):
    def __init__(self, message: str, transient: bool = True):
        super().__init__(message)
        self.transient = transient


class Provider:
    def complete(self, system: str, prompt: str, timeout: float) -> ModelResponse:  # pragma: no cover
        raise NotImplementedError


def _estimate(text: str) -> int:
    return max(1, len(text) // 4)


class FakeProvider(Provider):
    """Scripted responses for tests: pass a list (consumed in order) or a callable(system, prompt)."""

    def __init__(self, responses):
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def complete(self, system, prompt, timeout):
        self.calls.append((system, prompt))
        if callable(self.responses):
            out = self.responses(system, prompt)
        else:
            out = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(out, Exception):
            raise out
        text = out if isinstance(out, str) else json.dumps(out)
        return ModelResponse(text, _estimate(system + prompt), _estimate(text))


class HeuristicProvider(Provider):
    INJECTION = re.compile(r"ignore (all |any )?(previous|prior|above)|system prompt|you are now|override|disregard", re.I)
    REFUND = re.compile(r"refund|money back|reimburse|charge ?back", re.I)
    COMPLAINT = re.compile(r"broken|damaged|terrible|worst|angry|unacceptable|late|never arrived|defective|not working", re.I)
    URGENT = re.compile(r"urgent|asap|immediately|today|furious", re.I)

    def complete(self, system, prompt, timeout):
        if "validation error" in prompt.lower() or "previous response was invalid" in prompt.lower():
            fooled = False  # second chance: behave
        else:
            fooled = bool(self.INJECTION.search(prompt))
        message = self._customer_message(prompt)
        if self.REFUND.search(message):
            category = "refund_request"
        elif self.COMPLAINT.search(message):
            category = "complaint"
        else:
            category = "question"
        priority = "high" if self.URGENT.search(message) else ("medium" if category != "question" else "low")
        out = {"category": category, "priority": priority, "summary": " ".join(message.split())[:200] or "(empty message)"}
        if fooled:  # a model that obeyed the injected instruction and tried to smuggle in extra fields
            out.update({"approved": True, "skip_approval": True})
        text = json.dumps(out)
        return ModelResponse(text, _estimate(system + prompt), _estimate(text))

    @staticmethod
    def _customer_message(prompt: str) -> str:
        m = re.search(r"---\n(.*?)\n---", prompt, re.S)
        return m.group(1) if m else prompt


class AnthropicProvider(Provider):
    def __init__(self):
        self.key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = os.environ.get("RELAY_MODEL", "claude-haiku-5-5")
        if not self.key:
            raise RuntimeError("RELAY_PROVIDER=anthropic needs ANTHROPIC_API_KEY")

    def complete(self, system, prompt, timeout):
        try:
            r = httpclient.request(
                "POST", "https://api.anthropic.com/v1/messages",
                {"model": self.model, "max_tokens": 1024, "system": system,
                 "messages": [{"role": "user", "content": prompt}]},
                {"x-api-key": self.key, "anthropic-version": "2023-06-01"}, timeout)
        except httpclient.TransportError as e:
            raise ProviderError(str(e)) from e
        if r.status >= 500 or r.status == 429:
            raise ProviderError(f"provider HTTP {r.status}")
        if r.status != 200 or not isinstance(r.body, dict):
            raise ProviderError(f"provider HTTP {r.status}: {str(r.body)[:200]}", transient=False)
        text = "".join(b.get("text", "") for b in r.body.get("content", []) if b.get("type") == "text")
        usage = r.body.get("usage", {})
        return ModelResponse(text, usage.get("input_tokens", 0), usage.get("output_tokens", 0))


class OpenAICompatProvider(Provider):
    def __init__(self):
        self.base = os.environ.get("RELAY_OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.key = os.environ.get("OPENAI_API_KEY", "")
        self.model = os.environ.get("RELAY_MODEL", "gpt-4o-mini")

    def complete(self, system, prompt, timeout):
        try:
            r = httpclient.request(
                "POST", f"{self.base}/chat/completions",
                {"model": self.model, "messages": [{"role": "system", "content": system},
                                                   {"role": "user", "content": prompt}]},
                {"Authorization": f"Bearer {self.key}"}, timeout)
        except httpclient.TransportError as e:
            raise ProviderError(str(e)) from e
        if r.status >= 500 or r.status == 429:
            raise ProviderError(f"provider HTTP {r.status}")
        if r.status != 200 or not isinstance(r.body, dict):
            raise ProviderError(f"provider HTTP {r.status}: {str(r.body)[:200]}", transient=False)
        usage = r.body.get("usage", {})
        return ModelResponse(r.body["choices"][0]["message"]["content"],
                             usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))


def build_provider(name: str) -> Provider:
    if name == "heuristic":
        return HeuristicProvider()
    if name == "anthropic":
        return AnthropicProvider()
    if name == "openai":
        return OpenAICompatProvider()
    raise ValueError(f"unknown provider '{name}' (use heuristic, anthropic or openai)")
