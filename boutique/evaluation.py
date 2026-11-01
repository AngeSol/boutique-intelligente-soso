"""Évaluer une réponse AVANT de l'envoyer au client.

1. Règles en code (gratuites, déterministes) :
   - chiffres_non_sources : tout nombre de la réponse doit venir d'un outil ;
   - regle_total : total = articles + livraison, et c'est bien ce total qui est annoncé.
2. Un LLM juge (payant, plus subtil) : exactitude, respect des règles, ton, format WhatsApp.
"""
from __future__ import annotations

import json
import re

from .prompts import JUGE

_NOMBRE = re.compile(r"\d{1,3}(?:[  .]\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?")


def formater_whatsapp(texte: str) -> str:
    """Corrige en code ce que le LLM oublie souvent : WhatsApp n'est pas du Markdown.
    **gras** → *gras*, titres # supprimés, espaces de fin de ligne retirés."""
    texte = re.sub(r"\*\*(.+?)\*\*", r"*\1*", texte)
    texte = re.sub(r"__(.+?)__", r"_\1_", texte)
    texte = re.sub(r"^\s{0,3}#{1,6}\s*", "", texte, flags=re.MULTILINE)
    texte = "\n".join(l.rstrip() for l in texte.splitlines())
    return re.sub(r"\n{3,}", "\n\n", texte).strip()


def valeurs(texte: str) -> set[float]:
    """Extrait les nombres d'un texte : '25 000', '25.000', '38,11', '51500'."""
    vals = set()
    for brut in _NOMBRE.findall(texte):
        s = re.sub(r"[  ]", "", brut)
        if re.fullmatch(r"\d{1,3}(\.\d{3})+", s):  # 25.000 = 25000
            s = s.replace(".", "")
        s = s.replace(",", ".")
        try:
            vals.add(round(float(s), 2))
        except ValueError:
            pass
    return vals


def chiffres_non_sources(reponse: str, resultats_outils: list, textes_client: list[str] = ()) -> list[float]:
    """Nombres >= 100 de la réponse absents des résultats d'outils et des messages du client
    (le client peut donner son numéro de téléphone, par exemple)."""
    client = " ".join(textes_client)
    client += " " + re.sub(r"(?<=\d)[ .\-](?=\d)", "", client)  # « 97 11 22 33 » compte aussi comme 97112233
    sources = valeurs(json.dumps(resultats_outils, ensure_ascii=False) + " " + client)

    def connu(v):  # tolérance pour les arrondis de conversion (38.11 vs 38.1)
        return any(abs(v - s) <= max(0.06, s * 0.001) for s in sources)

    return sorted(v for v in valeurs(reponse) if v >= 100 and not connu(v))


def regle_total(reponse: str, resultats: list[dict]) -> dict:
    """Règle métier : Total = articles + livraison, annoncé tel que calculé par l'outil.

    resultats : [{"outil": nom, "resultat": {...}}, ...]
    """
    devis = [r["resultat"] for r in resultats
             if r["outil"] == "calculer_livraison" and "total_xof" in r["resultat"]]
    parle_de_total = re.search(r"\btotal\b", reponse, re.IGNORECASE) is not None
    commande_faite = any(r["outil"] == "creer_commande" and "total_xof" in r["resultat"] for r in resultats)

    if not devis:
        if parle_de_total and not commande_faite:
            return {"ok": False, "detail": "La réponse annonce un total sans avoir appelé calculer_livraison."}
        return {"ok": True, "detail": "aucun total à vérifier"}

    d = devis[-1]
    somme_lignes = sum(l["prix_unitaire_xof"] * l["quantite"] for l in d["lignes"])
    if somme_lignes != d["sous_total_articles_xof"] or somme_lignes + d["frais_livraison_xof"] != d["total_xof"]:
        return {"ok": False, "detail": "Incohérence dans le devis (articles + livraison ≠ total)."}
    if parle_de_total and d["total_xof"] not in valeurs(reponse):
        return {"ok": False,
                "detail": f"Le total annoncé doit être exactement {d['total_xof']} FCFA "
                          f"({d['sous_total_articles_xof']} articles + {d['frais_livraison_xof']} livraison)."}
    return {"ok": True, "detail": f"total {d['total_xof']} = {d['sous_total_articles_xof']} + {d['frais_livraison_xof']}"}


def evaluer_regles(reponse: str, resultats: list[dict], textes_client: list[str]) -> dict:
    """Regroupe les règles en code. Renvoie {"ok": bool, "problemes": [str]}."""
    problemes = []
    suspects = chiffres_non_sources(reponse, [r["resultat"] for r in resultats], textes_client)
    if suspects:
        problemes.append(f"Chiffres qui ne viennent d'aucun outil : {suspects}. "
                         "Appelle l'outil qui les donne, ou retire-les.")
    total = regle_total(reponse, resultats)
    if not total["ok"]:
        problemes.append(total["detail"])
    return {"ok": not problemes, "problemes": problemes}


class JugeLLM:
    """Un second LLM note la réponse de 1 à 5. On peut lui donner un modèle différent."""

    def __init__(self, llm, model: str | None = None):
        self.llm = llm
        self.model = model

    async def noter(self, conversation: list[dict], reponse: str, resultats: list[dict]) -> dict:
        echange = "\n".join(f"{m['role']}: {m['content']}" for m in conversation
                            if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str))
        contenu = (f"## Conversation\n{echange}\n\n"
                   f"## Données renvoyées par les outils\n{json.dumps(resultats, ensure_ascii=False)[:6000]}\n\n"
                   f"## Réponse proposée par la vendeuse\n{reponse}")
        rep = await self.llm.chat([{"role": "system", "content": JUGE}, {"role": "user", "content": contenu}],
                                  temperature=0, model=self.model)
        texte = rep.message.get("content") or ""
        m = re.search(r"\{.*\}", texte, re.DOTALL)
        try:
            avis = json.loads(m.group(0)) if m else {}
            note = int(avis.get("note"))
        except (ValueError, TypeError, json.JSONDecodeError):
            return {"note": None, "commentaire": f"Juge illisible : {texte[:200]}", "usage": rep.usage}
        return {"note": max(1, min(5, note)), "respecte_regles": bool(avis.get("respecte_regles", True)),
                "commentaire": str(avis.get("commentaire", ""))[:400], "usage": rep.usage}
