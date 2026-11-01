"""Palier 1 : discuter avec l'agent vendeur dans le terminal.

Usage :
    python palier1.py              # conversation libre
    python palier1.py --demo       # les 3 tests imposés du palier 1
    python palier1.py --check      # vérifie la clé RodiumAI et le tool calling
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from boutique.agent import AgentVendeur
from boutique.config import Settings
from boutique.llm import LLMError, RodiumClient
from boutique.store import JsonFileStore
from boutique.tools import SCHEMAS, Toolbox

# Les 3 tests imposés du palier 1 (sujet)
DEMO = [
    "Vous avez des pagnes ? Combien ça fait en euros pour ma sœur à Paris ?",
    "Livrez-moi à Lomé.",
    "C'est combien le sac en raphia ?",
]


def construire_agent() -> AgentVendeur:
    s = Settings.from_env()
    llm = RodiumClient(s.api_key, s.base_url, s.model)
    return AgentVendeur(llm, Toolbox(JsonFileStore("data")), max_steps=s.max_steps)


async def verifier_connexion() -> None:
    s = Settings.from_env()
    llm = RodiumClient(s.api_key, s.base_url, s.model)
    print(f"Modèle : {s.model}  ·  URL : {s.base_url}")
    rep = await llm.chat([{"role": "user", "content": "Vous avez un sac en raphia ?"}],
                         tools=SCHEMAS)
    appels = rep.message.get("tool_calls") or []
    if appels:
        print("✅ Tool calling OK →", appels[0]["function"]["name"], appels[0]["function"]["arguments"])
    else:
        print("⚠️ Le modèle a répondu sans appeler d'outil :", rep.message.get("content"))
        print("   Essaie un autre modèle dans LLM_MODEL.")


async def tour(agent: AgentVendeur, historique: list[dict], texte: str) -> None:
    historique.append({"role": "user", "content": texte})
    rep = await agent.repondre(historique)
    historique.append(rep.message_historique())
    print(rep.trace.afficher())
    print(f"\n🛍️  Awa : {rep.texte}\n")
    chemin = rep.trace.sauvegarder("traces", question=texte, reponse=rep.texte)
    print(f"(trace enregistrée : {chemin})\n")


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--demo", action="store_true")
    p.add_argument("--check", action="store_true")
    a = p.parse_args()
    try:
        if a.check:
            await verifier_connexion()
            return
        agent = construire_agent()
        historique: list[dict] = []
        if a.demo:
            for i, q in enumerate(DEMO, 1):  # chaque test est une conversation indépendante
                print(f"\n{'═' * 70}\nTEST {i}/3 · 👤 Client : {q}")
                await tour(agent, [], q)
            return
        print("Boutique Pagnes de Dantokpa — tape 'quit' pour sortir.\n")
        while True:
            texte = input("👤 Vous : ").strip()
            if texte.lower() in {"quit", "exit", "q"}:
                break
            if texte:
                await tour(agent, historique, texte)
    except LLMError as e:
        print(f"❌ {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
