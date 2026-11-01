"""Équipe multi-agents (palier 2).

            ┌──────────── Gérant (coordinateur, ne parle pas au client)
            │                 │                     │
     Vendeur              Logisticien           Rédacteur
  chercher_produit      calculer_livraison    écrit le message
  verifier_stock        convertir_devise      WhatsApp final
  creer_commande

Chaque membre ne voit que SES outils. Le Rédacteur reçoit les résultats bruts des
outils (les « faits ») pour ne pas dépendre des résumés. Même évaluation que l'agent seul.
"""
from __future__ import annotations

import json

from .agent import (MESSAGE_LIMITE, MESSAGE_VERIFICATION, ReponseAgent, Trace, boucle_outils, pour_le_llm,
                    textes_du_client)
from .evaluation import evaluer_regles, formater_whatsapp
from .prompts import SYSTEM_GERANT, SYSTEM_LOGISTICIEN, SYSTEM_REDACTEUR, SYSTEM_VENDEUR_EQUIPE
from .tools import SCHEMAS, Toolbox


def _delegation(nom: str, description: str) -> dict:
    return {"type": "function", "function": {
        "name": nom, "description": description,
        "parameters": {"type": "object", "properties": {
            "consigne": {"type": "string", "description": "Consigne précise et complète (le membre ne voit pas la conversation)."}},
            "required": ["consigne"]}}}


SCHEMAS_GERANT = [
    _delegation("deleguer_vendeur", "Produits, prix, stock, alternatives, enregistrement des commandes."),
    _delegation("deleguer_logisticien", "Frais de livraison, total (articles + livraison), conversion de devises."),
    _delegation("deleguer_redacteur", "Rédige le message WhatsApp final pour le client. À appeler en dernier."),
]
OUTILS_VENDEUR = {"chercher_produit", "verifier_stock", "creer_commande"}
OUTILS_LOGISTICIEN = {"calculer_livraison", "convertir_devise"}


class EquipeBoutique:
    def __init__(self, llm, toolbox: Toolbox, max_delegations: int = 4, max_steps_membre: int = 4,
                 juge=None, seuil_juge: int = 3):
        self.llm = llm
        self.tools = toolbox
        self.max_delegations = max_delegations
        self.max_steps_membre = max_steps_membre
        self.juge = juge
        self.seuil_juge = seuil_juge

    # ------------------------------------------------------------ membres
    async def _membre(self, nom: str, system: str, autorises: set[str], consigne: str,
                      resultats: list[dict], trace: Trace) -> str:
        schemas = [s for s in SCHEMAS if s["function"]["name"] in autorises]

        async def executer(outil, arguments):
            if outil not in autorises:
                return {"erreur": f"L'outil {outil} n'est pas autorisé pour le {nom}."}
            return await self.tools.run(outil, arguments)

        messages = [{"role": "system", "content": system}, {"role": "user", "content": consigne}]
        texte = await boucle_outils(self.llm, messages, schemas, executer, self.max_steps_membre,
                                    trace, nom, resultats)
        return texte or "Aucun résultat (limite d'étapes atteinte)."

    async def _rediger(self, historique: list[dict], consigne: str, resultats: list[dict],
                       trace: Trace, corrections: str | None = None) -> str:
        conversation = "\n".join(f"{m['role']}: {m['content']}" for m in pour_le_llm(historique)[-8:]
                                 if isinstance(m.get("content"), str))
        contenu = (f"## Conversation\n{conversation}\n\n## Consigne du Gérant\n{consigne}\n\n"
                   f"## FAITS (résultats des outils)\n{json.dumps(resultats, ensure_ascii=False)[:8000]}")
        if corrections:
            contenu += f"\n\n## Corrections obligatoires\n{corrections}"
        rep = await self.llm.chat([{"role": "system", "content": SYSTEM_REDACTEUR},
                                   {"role": "user", "content": contenu}], temperature=0.4)
        trace.compter(rep.usage)
        texte = (rep.message.get("content") or "").strip()
        trace.ajouter(type="redaction", agent="rédacteur", detail=f"{len(texte)} caractères"
                      + (" (correction)" if corrections else ""))
        return texte

    # ------------------------------------------------------------ orchestration
    async def repondre(self, historique: list[dict]) -> ReponseAgent:
        self.tools.nouveau_tour()
        trace, resultats = Trace(), []
        messages = [{"role": "system", "content": SYSTEM_GERANT}, *pour_le_llm(historique)]
        final, consigne_finale = None, ""

        for _ in range(self.max_delegations + 1):
            rep = await self.llm.chat(messages, tools=SCHEMAS_GERANT)
            trace.compter(rep.usage)
            msg = rep.message
            appels = msg.get("tool_calls") or []
            if not appels:  # le Gérant a répondu sans déléguer : on passe quand même par le Rédacteur
                consigne_finale = msg.get("content") or "Réponds au client."
                final = await self._rediger(historique, consigne_finale, resultats, trace)
                break

            messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": appels})
            for appel in appels:
                nom = appel["function"]["name"]
                try:
                    consigne = json.loads(appel["function"].get("arguments") or "{}").get("consigne", "")
                except json.JSONDecodeError:
                    consigne = ""
                if nom == "deleguer_redacteur":
                    consigne_finale = consigne
                    final = await self._rediger(historique, consigne, resultats, trace)
                    compte_rendu = "Message rédigé et envoyé au client."
                elif nom == "deleguer_vendeur":
                    compte_rendu = await self._membre("vendeur", SYSTEM_VENDEUR_EQUIPE, OUTILS_VENDEUR,
                                                      consigne, resultats, trace)
                elif nom == "deleguer_logisticien":
                    compte_rendu = await self._membre("logisticien", SYSTEM_LOGISTICIEN, OUTILS_LOGISTICIEN,
                                                      consigne, resultats, trace)
                else:
                    compte_rendu = f"Membre inconnu : {nom}"
                trace.ajouter(type="delegation", agent="gérant", vers=nom.replace("deleguer_", ""),
                              consigne=consigne, compte_rendu=compte_rendu)
                messages.append({"role": "tool", "tool_call_id": appel["id"], "content": compte_rendu})
            if final is not None:
                break

        if final is None:
            trace.ajouter(type="limite_atteinte", detail=f"max_delegations={self.max_delegations}")
            return ReponseAgent(MESSAGE_LIMITE, trace)

        # Évaluation avant envoi : mêmes règles que l'agent seul
        client = textes_du_client(historique)
        for tentative in range(2):
            ev = evaluer_regles(final, resultats, client)
            trace.ajouter(type="evaluation", agent="règles", verdict="✅ règles OK" if ev["ok"] else "❌ règles",
                          detail=" | ".join(ev["problemes"]))
            if ev["ok"]:
                break
            if tentative == 1:
                trace.ajouter(type="reponse_finale", detail="message de vérification (échec après correction)")
                return ReponseAgent(MESSAGE_VERIFICATION, trace)
            final = await self._rediger(historique, consigne_finale, resultats, trace, "\n".join(ev["problemes"]))

        if self.juge is not None:
            avis = await self.juge.noter(historique, final, resultats)
            trace.compter(avis.get("usage"))
            trace.ajouter(type="evaluation", agent="juge", verdict=f"⚖️ note {avis['note']}/5",
                          detail=avis.get("commentaire", ""))
            if avis["note"] is not None and avis["note"] < self.seuil_juge:
                nouveau = await self._rediger(historique, consigne_finale, resultats, trace,
                                              f"Un relecteur a noté {avis['note']}/5 : {avis['commentaire']}")
                if evaluer_regles(nouveau, resultats, client)["ok"]:
                    final = nouveau

        final = formater_whatsapp(final)
        trace.ajouter(type="reponse_finale", detail=f"{len(final)} caractères")
        return ReponseAgent(final, trace)
