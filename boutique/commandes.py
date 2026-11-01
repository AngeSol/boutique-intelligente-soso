"""Cycle de vie d'une commande et validation par la gérante (palier 2-3).

  en_attente_validation_gerante ──OUI d'Afi──▶ validee  (le stock baisse ici)
                                └─NON d'Afi─▶ refusee
                                └─stock épuisé entre-temps─▶ refusee (motif stock)

C'est du code, pas du LLM : aucun client ne peut valider une commande
en « convainquant » l'agent.
"""
from __future__ import annotations

from datetime import datetime, timezone

EN_ATTENTE = "en_attente_validation_gerante"
VALIDEE = "validee"
REFUSEE = "refusee"


def _maintenant() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fcfa(montant: float) -> str:
    return f"{int(round(montant)):,}".replace(",", " ") + " FCFA"


async def valider_commande(store, commande_id: str, par: str = "gérante") -> dict:
    commande = await store.get_order(commande_id)
    if commande is None:
        return {"erreur": f"Commande {commande_id} introuvable."}
    if commande["statut"] != EN_ATTENTE:
        return {"erreur": f"Commande {commande_id} déjà traitée ({commande['statut']}).", "commande": commande}

    catalogue = await store.get_catalogue()
    index = {p["id"]: p for p in catalogue["produits"]}
    manquants = [l["nom"] for l in commande["lignes"]
                 if index.get(l["produit_id"], {}).get("stock", 0) < l["quantite"]]
    if manquants:
        commande.update(statut=REFUSEE, traitee_le=_maintenant(), traitee_par="système",
                        motif=f"Stock insuffisant : {', '.join(manquants)}")
        await store.save_order(commande)
        return {"erreur": commande["motif"], "commande": commande}

    for ligne in commande["lignes"]:  # le stock ne baisse qu'à la validation
        index[ligne["produit_id"]]["stock"] -= ligne["quantite"]
    await store.save_catalogue(catalogue)
    commande.update(statut=VALIDEE, traitee_le=_maintenant(), traitee_par=par)
    await store.save_order(commande)
    return {"ok": True, "commande": commande}


async def refuser_commande(store, commande_id: str, motif: str = "", par: str = "gérante") -> dict:
    commande = await store.get_order(commande_id)
    if commande is None:
        return {"erreur": f"Commande {commande_id} introuvable."}
    if commande["statut"] != EN_ATTENTE:
        return {"erreur": f"Commande {commande_id} déjà traitée ({commande['statut']}).", "commande": commande}
    commande.update(statut=REFUSEE, traitee_le=_maintenant(), traitee_par=par,
                    motif=motif or "non précisé")
    await store.save_order(commande)
    return {"ok": True, "commande": commande}


async def commandes_en_attente(store) -> list[dict]:
    return [o for o in await store.list_orders() if o["statut"] == EN_ATTENTE]


# ------------------------------------------------------------------ messages
def recap_gerante(c: dict) -> str:
    """Format du sujet : « Commande #12 : 2 robes en wax, Calavi, 46 000 FCFA. Répondez OUI 12 ou NON 12 »."""
    articles = ", ".join(f"{l['quantite']} × {l['nom']}" for l in c["lignes"])
    return (f"🧾 *Commande #{c['id']}* : {articles}, {c['zone']}, *{fcfa(c['total_xof'])}*.\n"
            f"Client : {c['client']['nom']} ({c['client'].get('telephone') or 'numéro inconnu'})\n"
            f"Répondez *OUI {c['id']}* ou *NON {c['id']}* (suivi d'un motif si vous voulez).")


def message_client_decision(c: dict) -> str:
    if c["statut"] == VALIDEE:
        return (f"✅ Bonne nouvelle ! Votre commande *#{c['id']}* est validée par la boutique.\n"
                f"Total : *{fcfa(c['total_xof'])}* · Livraison {c['zone']} ({c['delai']}).\n"
                f"Merci pour votre confiance 🙏")
    return (f"😔 Désolée, votre commande *#{c['id']}* n'a pas pu être validée par la boutique"
            f" ({c.get('motif', 'motif non précisé')}).\n"
            f"Voulez-vous que je vous propose une autre option ?")
