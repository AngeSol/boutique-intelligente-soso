"""Palier 3 en local : simule WhatsApp (client ET gérante) avec le vrai service et le vrai LLM.

Utile pour tout tester AVANT de déployer sur Cloudflare. Les messages « envoyés »
s'affichent dans le terminal au lieu de partir sur WhatsApp.

    python simulateur_whatsapp.py

Tape un message comme le client. Préfixe par « g: » pour écrire en tant que gérante :
    Bonjour, vous avez du Woodin ?
    g: OK CMD-20261031-AB12
    g: COMMANDES
"""
from __future__ import annotations

import asyncio
import itertools
import time

from boutique.config import Settings
from boutique.greenapi import GreenAPIConsole
from boutique.llm import LLMError, RodiumClient
from boutique.store import JsonFileStore
from boutique.whatsapp import ServiceWhatsApp

CLIENT = "22990000001@c.us"
GERANTE = "22990000099@c.us"


def webhook(chat: str, texte: str, n: int) -> dict:
    """Construit un webhook identique à celui de Green API."""
    return {"typeWebhook": "incomingMessageReceived", "idMessage": f"SIMU{time.time_ns()}-{n}",
            "senderData": {"chatId": chat, "sender": chat, "senderName": "Simulateur"},
            "messageData": {"typeMessage": "textMessage", "textMessageData": {"textMessage": texte}}}


async def main() -> None:
    s = Settings.from_env()
    s.gerante_chat_id = GERANTE
    try:
        llm = RodiumClient(s.api_key, s.base_url, s.model)
    except LLMError as e:
        print(f"❌ {e}")
        return
    service = ServiceWhatsApp(JsonFileStore("data"), GreenAPIConsole(), s, llm)
    print(f"Simulateur WhatsApp · client {CLIENT} · gérante {GERANTE} (préfixe « g: »). 'quit' pour sortir.\n")
    for n in itertools.count(1):
        ligne = (await asyncio.to_thread(input, "> ")).strip()
        if ligne.lower() in {"quit", "exit", "q"}:
            break
        if not ligne:
            continue
        chat, texte = (GERANTE, ligne[2:].strip()) if ligne.lower().startswith("g:") else (CLIENT, ligne)
        statut = await service.traiter_sans_erreur(webhook(chat, texte, n))
        print(f"   (statut : {statut})")


if __name__ == "__main__":
    asyncio.run(main())
