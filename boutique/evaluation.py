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


_DEVISE = r"(?:\s*(?:F\s?CFA|FCFA|XOF|francs?|€|EUR|euros?|\$|USD|dollars?|NGN|nairas?|₦|CAD|GBP|£))"
_MONTANT = re.compile(r"(\d{1,3}(?:[  .]\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?)" + _DEVISE, re.IGNORECASE)


def chiffres_non_sources(reponse: str, resultats_outils: list, textes_client: list[str] = ()) -> list[float]:
    """Nombres >= 100 de la réponse qui ne viennent d'aucun outil.

    - un MONTANT (suivi de FCFA, €, NGN…) doit venir d'un outil, jamais du message du client :
      « la robe est à 1 000 FCFA » ne rend pas 1 000 FCFA acceptable ;
    - un autre nombre (ex. un numéro de téléphone) peut venir du message du client.
    """
    outils = valeurs(json.dumps(resultats_outils, ensure_ascii=False))
    client = " ".join(textes_client)
    client += " " + re.sub(r"(?<=\d)[ .\-](?=\d)", "", client)  # « 97 11 22 33 » compte aussi comme 97112233
    tous = outils | valeurs(client)

    def connu(v, sources):  # tolérance pour les arrondis de conversion (38.11 vs 38.1)
        return any(abs(v - s) <= max(0.06, s * 0.001) for s in sources)

    montants = valeurs(" ".join(m.group(1) for m in _MONTANT.finditer(reponse)))
    suspects = {v for v in montants if v >= 100 and not connu(v, outils)}
    suspects |= {v for v in valeurs(reponse) - montants if v >= 100 and not connu(v, tous)}
    return sorted(suspects)


def regle_total(reponse: str, resultats: list[dict]) -> dict:
    """Règle métier : Total = articles + livraison, annoncé tel que calculé par l'outil.

    resultats : [{"outil": nom, "resultat": {...}}, ...]
    """
    devis = [r["resultat"] for r in resultats
             if r["outil"] in ("frais_livraison", "creer_commande") and "total_xof" in r["resultat"]
             and "lignes" in r["resultat"]]
    parle_de_total = re.search(r"\btotal\b", reponse, re.IGNORECASE) is not None
    commande_faite = any(r["outil"] == "creer_commande" and "total_xof" in r["resultat"] for r in resultats)

    if not devis:
        if parle_de_total and not commande_faite:
            return {"ok": False, "detail": "La réponse annonce un total sans l'avoir obtenu de frais_livraison (zone + articles)."}
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


def regle_livraison(reponse: str, resultats: list[dict], dernier_message: str) -> str | None:
    """Le client parle de livraison ⇒ frais_livraison doit avoir été appelé (pas de « je vais vérifier »)."""
    if re.search(r"\blivr", dernier_message, re.IGNORECASE) and not any(r["outil"] == "frais_livraison"
                                                                        for r in resultats):
        return ("Le client parle de livraison : appelle frais_livraison avec la zone (même sans articles) "
                "avant de répondre ; si la zone n'est pas desservie, liste les zones desservies.")
    return None


def regle_rupture(reponse: str, resultats: list[dict], dernier_message: str = "") -> str | None:
    """Le client demande un produit en rupture ⇒ la réponse donne son prix, signale la rupture et propose
    une alternative tirée des outils. (Seulement si le client parle de ce produit dans son dernier message.)"""
    ruptures = []
    for r in resultats:
        res = r["resultat"]
        lignes = res.get("resultats", []) if r["outil"] == "chercher_produits" else [res]
        lignes = lignes + res.get("articles_non_disponibles", [])
        for l in lignes:
            if (l.get("rupture") or l.get("disponible") is False) and l.get("alternatives"):
                ruptures.append(l)
    bas, demande = _sans_accents(reponse), _sans_accents(dernier_message)
    for l in ruptures:
        nom = l.get("nom", "")
        if nom and _sans_accents(nom.split()[0]) not in demande:
            continue  # le client ne parle pas de ce produit dans ce message (ex. « oui je confirme »)
        if l.get("prix_xof") and l["prix_xof"] not in valeurs(reponse):
            return (f"Donne aussi le prix de {nom or 'ce produit'} ({l['prix_xof']} FCFA), "
                    "puis signale la rupture et propose l'alternative.")
        noms = [a["nom"] for a in l["alternatives"]]
        if not any(_sans_accents(n.split()[0]) in bas for n in noms):
            return (f"{nom or 'Un produit'} est en rupture : dis-le et propose explicitement une "
                    f"alternative renvoyée par l'outil (par exemple : {', '.join(noms)}), avec son prix.")
    return None


_CONFIRMATION = re.compile(r"^\W*(oui|ok|d'accord|daccord|je confirme|c'est bon|valide|go)\b", re.IGNORECASE)


def _memo_du_recap(historique: list[dict]) -> str:
    """Note interne du dernier récapitulatif chiffré de la vendeuse."""
    for m in reversed(historique[:-1]):
        if m.get("role") == "assistant" and "total annoncé" in (m.get("memo") or ""):
            return m["memo"]
    return ""


def total_du_recap(historique: list[dict]) -> int | None:
    """Total annoncé dans le dernier récapitulatif de la vendeuse (lu dans sa note interne)."""
    t = re.search(r"total annoncé : (\d+)", _memo_du_recap(historique))
    return int(t.group(1)) if t else None


def articles_du_recap(historique: list[dict]) -> dict[str, int]:
    """Articles du dernier récapitulatif : {"P05": 2}."""
    return {pid: int(q) for pid, q in re.findall(r"\b(P\d+) = [^;·]*?× (\d+)", _memo_du_recap(historique))}


def regle_confirmation(resultats: list[dict], dernier_message: str, total_recap: int | None) -> str | None:
    """Un « oui » confirme EXACTEMENT le récapitulatif : même articles, même total.
    (Bug réel du 01/11 : après « oui je confirme », l'agent avait ajouté l'alternative → 55 000 au lieu de 46 000.)"""
    if total_recap is None or not _CONFIRMATION.search(dernier_message):
        return None
    totaux = [r["resultat"]["total_xof"] for r in resultats
              if r["outil"] in ("frais_livraison", "creer_commande") and "total_xof" in r["resultat"]]
    if totaux and totaux[-1] != total_recap:
        return (f"Le client a dit « oui » au récapitulatif à {total_recap} FCFA : il confirme exactement ces "
                f"articles. N'ajoute pas l'alternative (sauf demande explicite) et ne retire rien : "
                f"refais le calcul avec les seuls articles de la note interne (total {total_recap} FCFA).")
    return None


def regle_alternative_non_demandee(resultats: list[dict], textes_client: list[str]) -> str | None:
    """Une alternative proposée n'entre dans le total que si le client l'a demandée (bug réel du 01/11 :
    le juge avait poussé l'agent à compter les sandales → 55 000 au lieu de 46 000)."""
    alternatives = {}
    for r in resultats:
        res = r["resultat"]
        lignes = [res] + res.get("resultats", []) + res.get("articles_non_disponibles", [])
        for l in lignes:
            if l.get("rupture") or l.get("disponible") is False:
                alternatives |= {a["id"]: a["nom"] for a in l.get("alternatives", []) if "id" in a}
    devis = [r["resultat"] for r in resultats if r["outil"] in ("frais_livraison", "creer_commande")
             and "lignes" in r["resultat"]]
    if not devis or not alternatives:
        return None
    client = _sans_accents(" ".join(textes_client))
    for l in devis[-1]["lignes"]:
        nom = alternatives.get(l.get("produit_id"))
        if nom and _sans_accents(nom.split()[0]) not in client:
            return (f"{nom} n'est qu'une alternative proposée : le client ne l'a pas demandée, ne l'ajoute pas "
                    f"au total. Rappelle frais_livraison sans cet article et propose-le seulement en option.")
    return None


def _sans_accents(t: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()


def evaluer_regles(reponse: str, resultats: list[dict], textes_client: list[str],
                   total_recap: int | None = None) -> dict:
    """Règles en code. Renvoie {"ok": bool, "problemes": [str]}."""
    problemes = []
    suspects = chiffres_non_sources(reponse, [r["resultat"] for r in resultats], textes_client)
    if suspects:
        problemes.append(f"Chiffres qui ne viennent d'aucun outil : {suspects}. "
                         "Appelle l'outil qui les donne, ou retire-les.")
    total = regle_total(reponse, resultats)
    if not total["ok"]:
        problemes.append(total["detail"])
    for regle in (regle_livraison(reponse, resultats, textes_client[-1] if textes_client else ""),
                  regle_rupture(reponse, resultats, textes_client[-1] if textes_client else ""),
                  regle_confirmation(resultats, textes_client[-1] if textes_client else "", total_recap),
                  regle_alternative_non_demandee(resultats, textes_client)):
        if regle:
            problemes.append(regle)
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
            return {"verdict": "ok", "note": None, "commentaire": f"Juge illisible : {texte[:200]}", "usage": rep.usage}
        verdict = str(avis.get("verdict", "")).strip().lower()
        if verdict not in ("ok", "pas encore"):
            verdict = "ok" if note >= 3 else "pas encore"
        return {"verdict": verdict, "note": max(1, min(5, note)),
                "respecte_regles": bool(avis.get("respecte_regles", True)),
                "commentaire": str(avis.get("commentaire", ""))[:400], "usage": rep.usage}
