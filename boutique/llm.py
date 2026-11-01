"""Client LLM asynchrone pour RodiumAI (API compatible OpenAI /v1/chat/completions).

httpx en mode async : c'est la seule façon de faire des appels HTTP
dans un Python Worker Cloudflare, donc on l'utilise dès le palier 1.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResponse:
    message: dict                      # {"role": "assistant", "content": ..., "tool_calls": [...]}
    usage: dict = field(default_factory=dict)
    model: str = ""


class RodiumClient:
    def __init__(self, api_key: str, base_url: str, model: str, timeout: float = 60.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        if not api_key:
            raise LLMError("RODIUM_API_KEY manquante : copie .env.example en .env et mets ta clé.")
        self.model = model
        self.url = f"{base_url}/chat/completions"
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self.timeout = timeout
        self.transport = transport

    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   temperature: float = 0.2, model: str | None = None,
                   response_format: dict | None = None) -> LLMResponse:
        payload: dict = {"model": model or self.model, "messages": messages, "temperature": temperature}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_format:
            payload["response_format"] = response_format

        last_error = None
        for attempt in range(3):  # réessaie sur 429 / 5xx
            try:
                async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                    r = await client.post(self.url, headers=self.headers, json=payload)
                if r.status_code in (429, 500, 502, 503, 504):
                    last_error = f"HTTP {r.status_code}: {r.text[:200]}"
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if r.status_code >= 400:
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:500]}")
                data = r.json()
                return LLMResponse(
                    message=data["choices"][0]["message"],
                    usage=data.get("usage", {}) or {},
                    model=data.get("model", payload["model"]),
                )
            except httpx.HTTPError as e:
                last_error = repr(e)
                await asyncio.sleep(1.5 * (attempt + 1))
        raise LLMError(f"Échec de l'appel LLM après 3 essais : {last_error}")
