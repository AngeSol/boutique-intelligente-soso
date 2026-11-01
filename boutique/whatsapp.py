"""Traitement d'un webhook WhatsApp (palier 3), indépendant de Cloudflare.

Le Worker (worker/src/entry.py) ne fait que : vérifier le jeton, répondre 200 tout de suite,
puis appeler `ServiceWhatsApp.traiter_sans_erreur(body)` en arrière-plan.
Le même service tourne en local dans `simulateur_whatsapp.py` (avec un faux WhatsApp).

Garde-fous :
- anti-doublon : chaque idMessage n'est traité qu'une fois (clé vu:<id>, 2 jours) ;
- groupes et messages non texte ignorés, texte tronqué à 1 000 caractères ;
- limite de 30 messages / heure / client (protège les crédits LLM) ;
- la gérante est reconnue par son numéro (GERANTE_CHAT_ID) et ses commandes OK/NON sont
  lues par du CODE, jamais par le LLM : un client ne peut pas valider une commande ;
- le numéro du client vient de WhatsApp, pas de ce que le LLM écrit.
"""
from __future__ import annotations

import json
import re
import time

from .agent import AgentVendeur
from .commandes import (VALIDEE, commandes_en_attente, fcfa, message_client_decision, recap_gerante,
                        refuser_commande, valider_commande)
from .evaluation import JugeLLM
from .tools import Toolbox

TTL_DOUBLON = 2 * 24 * 3600
TTL_HISTORIQUE = 7 * 24 * 3600
MAX_CARACTERES = 1000

AIDE_GERANTE = ("👩🏾‍💼 Commandes gérante :\n"
                "• *OK CMD-...* pour valider\n"
                "• *NON CMD-... motif* pour refuser\n"
                "• *COMMANDES* pour voir celles en attente")

_RE_OK = re.compile(r"^\s*(?:ok|oui|valider?|valide)\s+(cmd-[\w-]+)\s*$", re.IGNORECASE)
_RE_NON = re.compile(r"^\s*(?:non|refuser?|refuse)\s+(cmd-[\w-]+)\s*(.*)$", re.IGNORECASE | re.DOTALL)


def extraire_texte(body: dict) -> str | None:
    md = body.get("messageData") or {}
    t = md.get("typeMessage")
    if t == "textMessage":
        return (md.get("textMessageData") or {}).get("textMessage")
    if t in ("extendedTextMessage", "quotedMessage"):
        return (md.get("extendedTextMessageData") or {}).get("text")
    return None


class ServiceWhatsApp:
    def __init__(self, store, green, settings, llm, http_transport=None,
                 max_historique: int = 12, limite_par_heure: int = 30):
        self.store = store
        self.green = green
        self.s = settings
        self.llm = llm
        self.http_transport = http_transport
        self.max_historique = max_historique
        self.limite_par_heure = limite_par_heure

    # ------------------------------------------------------------ point d'entrée
    async def traiter_sans_erreur(self, body: dict) -> str:
        try:
            return await self.traiter(body)
        except Exception as e:  # noqa: BLE001 — un webhook ne doit jamais faire planter le Worker
            print(f"[erreur webhook] {e!r}")
            return f"erreur:{e!r}"

    async def traiter(self, body: dict) -> str:
        if body.get("typeWebhook") != "incomingMessageReceived":
            return "ignore:type"
        id_message = body.get("idMessage")
        if not id_message:
            return "ignore:sans_id"
        if await self.store.kv_get(f"vu:{id_message}"):
            return "doublon"
        await self.store.kv_put(f"vu:{id_message}", "1", ttl=TTL_DOUBLON)

        chat = (body.get("senderData") or {}).get("chatId", "")
        if not chat or chat.endswith("@g.us"):
            return "ignore:groupe"
        texte = extraire_texte(body)
        if not texte or not texte.strip():
            await self.green.envoyer(chat, "Pour le moment, je ne lis que les messages écrits 🙏")
            return "ignore:non_texte"
        texte = texte.strip()[:MAX_CARACTERES]

        if self.s.gerante_chat_id and chat == self.s.gerante_chat_id:
            return await self._gerante(texte)
        return await self._client(chat, texte, (body.get("senderData") or {}).get("senderName", ""))

    # ------------------------------------------------------------ client
    async def _client(self, chat: str, texte: str, nom: str) -> str:
        cle_rl = f"rl:{chat}:{time.strftime('%Y%m%d%H', time.gmtime())}"
        compteur = int(await self.store.kv_get(cle_rl) or 0) + 1
        await self.store.kv_put(cle_rl, str(compteur), ttl=3600)
        if compteur > self.limite_par_heure:
            if compteur == self.limite_par_heure + 1:
                await self.green.envoyer(chat, "Vous avez envoyé beaucoup de messages 🙏 "
                                               "Je vous réponds avec plaisir d'ici une heure.")
            return "limite_client"

        if texte.lower() in {"recommencer", "reset", "nouvelle conversation"}:
            await self.store.kv_put(f"hist:{chat}", "[]", ttl=TTL_HISTORIQUE)
            await self.green.envoyer(chat, "C'est noté, on repart de zéro 😊 Que puis-je faire pour vous ?")
            return "reset"

        historique = json.loads(await self.store.kv_get(f"hist:{chat}") or "[]")
        historique.append({"role": "user", "content": texte})
        historique = historique[-self.max_historique:]

        tools = Toolbox(self.store, self.http_transport, notifier=self._notifier_gerante,
                        contexte={"chat_id": chat, "telephone": "+" + chat.split("@")[0]})
        juge = JugeLLM(self.llm, self.s.juge_model) if self.s.juge_actif else None
        agent = AgentVendeur(self.llm, tools, max_steps=self.s.max_steps, juge=juge)
        try:
            rep = await agent.repondre(historique)
            reponse, entree = rep.texte, rep.message_historique()
            print(rep.trace.afficher())  # visible avec `wrangler tail`
        except Exception as e:  # noqa: BLE001
            print(f"[erreur agent] {e!r}")
            reponse = "Désolée, j'ai un petit souci technique 🙏 Je reviens vers vous très vite."
            entree = {"role": "assistant", "content": reponse}

        historique.append(entree)
        await self.store.kv_put(f"hist:{chat}", json.dumps(historique[-self.max_historique:], ensure_ascii=False),
                                ttl=TTL_HISTORIQUE)
        await self.green.envoyer(chat, reponse)
        return "repondu"

    async def _notifier_gerante(self, commande: dict) -> None:
        if self.s.gerante_chat_id:
            await self.green.envoyer(self.s.gerante_chat_id, recap_gerante(commande))

    # ------------------------------------------------------------ gérante
    async def _gerante(self, texte: str) -> str:
        # la gérante copie souvent « *OK CMD-…* » depuis le récapitulatif : on retire le gras/italique WhatsApp
        texte = re.sub(r"[*_~`]", "", texte).strip()
        if m := _RE_OK.match(texte):
            res = await valider_commande(self.store, m.group(1).upper(), par="gérante (WhatsApp)")
            return await self._apres_decision(res)
        if m := _RE_NON.match(texte):
            res = await refuser_commande(self.store, m.group(1).upper(), m.group(2).strip(), par="gérante (WhatsApp)")
            return await self._apres_decision(res)
        if texte.strip().lower() in {"commandes", "liste", "en attente"}:
            attente = await commandes_en_attente(self.store)
            if not attente:
                await self.green.envoyer(self.s.gerante_chat_id, "✅ Aucune commande en attente.")
            else:
                lignes = [f"• *{c['id']}* · {c['client']['nom']} · {fcfa(c['total_xof'])}" for c in attente]
                await self.green.envoyer(self.s.gerante_chat_id,
                                         "🧾 En attente :\n" + "\n".join(lignes) + "\n\n" + AIDE_GERANTE)
            return "gerante:liste"
        await self.green.envoyer(self.s.gerante_chat_id, AIDE_GERANTE)
        return "gerante:aide"

    async def _apres_decision(self, res: dict) -> str:
        commande = res.get("commande")
        if commande is None or ("erreur" in res and "déjà traitée" in res["erreur"]):
            await self.green.envoyer(self.s.gerante_chat_id, f"⚠️ {res.get('erreur')}")
            return "gerante:erreur"
        chat_client = commande.get("client", {}).get("chat_id")
        if chat_client:
            message = message_client_decision(commande)
            await self.green.envoyer(chat_client, message)
            # on l'ajoute à l'historique : Awa saura que la gérante a tranché
            historique = json.loads(await self.store.kv_get(f"hist:{chat_client}") or "[]")
            historique.append({"role": "assistant", "content": message,
                               "memo": f"commande {commande['id']} : {commande['statut']} par la gérante"})
            await self.store.kv_put(f"hist:{chat_client}",
                                    json.dumps(historique[-self.max_historique:], ensure_ascii=False),
                                    ttl=TTL_HISTORIQUE)
        if commande["statut"] == VALIDEE:
            info = f"✅ {commande['id']} validée. Stock mis à jour, client prévenu."
        else:
            info = f"❌ {commande['id']} refusée ({commande.get('motif')}). Client prévenu."
        await self.green.envoyer(self.s.gerante_chat_id, info)
        return f"gerante:{commande['statut']}"
