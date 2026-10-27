"""Model provider layer (Anthropic Messages API over HTTPS, with streaming
and tool use). Swappable: anything implementing ``stream_round`` works,
which is how the tests run without a network or an API key.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Iterator, Protocol

import requests

from app import config


class ProviderNotConfigured(Exception):
    pass


class ProviderError(Exception):
    pass


class Round(dict):
    """Result of one model round: {'content': [...blocks], 'stop_reason': str}."""


class Provider(Protocol):
    def stream_round(
        self, system: str, messages: list[dict], tools: list[dict], on_text: Callable[[str], None], fast: bool = False,
        max_tokens: int = 4096,
    ) -> Round: ...


class AnthropicProvider:
    API_VERSION = "2023-06-01"

    def _headers(self) -> dict:
        key = config.settings.anthropic_api_key
        if not key:
            raise ProviderNotConfigured("ANTHROPIC_API_KEY is not set")
        return {
            "x-api-key": key,
            "anthropic-version": self.API_VERSION,
            "content-type": "application/json",
        }

    def stream_round(self, system, messages, tools, on_text, fast=False, max_tokens=4096) -> Round:
        body: dict[str, Any] = {
            "model": config.settings.anthropic_fast_model if fast else config.settings.anthropic_model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "stream": True,
        }
        if tools:
            body["tools"] = tools
        try:
            resp = requests.post(
                f"{config.settings.anthropic_base_url}/v1/messages",
                headers=self._headers(), json=body, stream=True, timeout=(15, 300),
            )
        except requests.RequestException as exc:
            raise ProviderError(f"connection failed: {exc}") from exc
        if resp.status_code != 200:
            text = resp.text[:500]
            raise ProviderError(f"provider returned {resp.status_code}: {text}")
        return _parse_stream(resp.iter_lines(decode_unicode=True), on_text)


def _parse_stream(lines: Iterator[str], on_text: Callable[[str], None]) -> Round:
    blocks: list[dict] = []
    partial_json: dict[int, str] = {}
    stop_reason = None
    event = None
    for raw in lines:
        if raw is None:
            continue
        line = raw.strip()
        if not line:
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
            continue
        if not line.startswith("data:"):
            continue
        data = json.loads(line[5:].strip())
        kind = data.get("type", event)
        if kind == "content_block_start":
            idx = data["index"]
            block = dict(data["content_block"])
            while len(blocks) <= idx:
                blocks.append({})
            if block.get("type") == "tool_use":
                block["input"] = {}
                partial_json[idx] = ""
            if block.get("type") == "text":
                block["text"] = block.get("text", "")
            blocks[idx] = block
        elif kind == "content_block_delta":
            idx = data["index"]
            delta = data["delta"]
            if delta.get("type") == "text_delta":
                blocks[idx]["text"] += delta["text"]
                on_text(delta["text"])
            elif delta.get("type") == "input_json_delta":
                partial_json[idx] = partial_json.get(idx, "") + delta.get("partial_json", "")
        elif kind == "content_block_stop":
            idx = data["index"]
            if idx in partial_json:
                raw_json = partial_json.pop(idx)
                blocks[idx]["input"] = json.loads(raw_json) if raw_json.strip() else {}
        elif kind == "message_delta":
            stop_reason = data.get("delta", {}).get("stop_reason") or stop_reason
        elif kind == "error":
            raise ProviderError(str(data.get("error")))
    # keep only blocks the API accepts back in history
    clean = [b for b in blocks if b.get("type") in ("text", "tool_use") and (b.get("type") != "text" or b["text"])]
    return Round(content=clean, stop_reason=stop_reason or "end_turn")


_provider: Provider = AnthropicProvider()


def provider() -> Provider:
    return _provider


def set_provider(p: Provider) -> None:
    global _provider
    _provider = p


def complete(prompt: str, system: str = "", fast: bool = True, max_tokens: int = 1500) -> str:
    """One-shot text completion (no tools)."""
    parts: list[str] = []
    r = provider().stream_round(system or "You are a helpful assistant.", [{"role": "user", "content": prompt}], [], parts.append, fast=fast, max_tokens=max_tokens)
    return "".join(b.get("text", "") for b in r["content"] if b.get("type") == "text").strip()


def complete_json(prompt: str, system: str = "", fast: bool = True, max_tokens: int = 2000) -> Any:
    text = complete(prompt + "\n\nReply with only valid JSON, no prose, no code fences.", system, fast, max_tokens)
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1:] if "\n" in text else text
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    end = max(text.rfind("}"), text.rfind("]"))
    if start < 0 or end < start:
        raise ProviderError("model did not return JSON")
    return json.loads(text[start:end + 1])
