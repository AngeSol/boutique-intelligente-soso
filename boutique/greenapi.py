"""Client Green API minimal : envoyer un message WhatsApp texte."""
from __future__ import annotations

import httpx


class GreenAPI:
    def __init__(self, api_url: str, id_instance: str, token: str,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.url = f"{api_url.rstrip('/')}/waInstance{id_instance}/sendMessage/{token}"
        self.transport = transport

    async def envoyer(self, chat_id: str, message: str) -> dict:
        async with httpx.AsyncClient(timeout=15, transport=self.transport) as client:
            r = await client.post(self.url, json={"chatId": chat_id, "message": message[:4000]})
        if r.status_code >= 400:
            raise RuntimeError(f"Green API HTTP {r.status_code}: {r.text[:200]}")
        return r.json()


class GreenAPIConsole:
    """Remplaçant local : affiche les messages au lieu de les envoyer (simulateur)."""

    def __init__(self):
        self.envoyes: list[tuple[str, str]] = []

    async def envoyer(self, chat_id: str, message: str) -> dict:
        self.envoyes.append((chat_id, message))
        print(f"\n📲 WhatsApp → {chat_id}\n{message}\n")
        return {"idMessage": f"LOCAL{len(self.envoyes)}"}
