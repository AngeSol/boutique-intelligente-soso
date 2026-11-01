"""Agent vendeur seul (palier 1, fiabilisé au palier 2).

Boucle : le LLM choisit des outils → on les exécute → on recommence, jusqu'à une réponse.
Garde-fous :
- limite d'étapes (MAX_STEPS, 8 par défaut) : nombre maximal d'appels LLM par message client ;
- trace lisible de chaque étape (agent, outil, arguments, résultat, durée, tokens) ;
- évaluation avant de répondre : règles en code (chiffres sourcés, total) puis LLM juge (optionnel).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .evaluation import (_CONFIRMATION, articles_du_recap, chiffres_non_sources, evaluer_regles,  # noqa: F401
                         formater_whatsapp, total_du_recap)
from .prompts import CORRECTION, NUMERO_CONNU, REECRITURE_JUGE, SYSTEM_VENDEUR
from .tools import SCHEMAS, Toolbox

MESSAGE_LIMITE = ("Je suis désolée, je n'arrive pas à finaliser votre demande pour le moment. "
                  "Je transmets à la gérante qui vous répondra rapidement 🙏")
MESSAGE_VERIFICATION = ("Je préfère vérifier ces informations avec la gérante avant de vous répondre, "
                        "pour ne pas vous donner de mauvais chiffres. Je reviens vers vous très vite 🙏")


# ---------------------------------------------------------------- trace
@dataclass
class Trace:
    etapes: list[dict] = field(default_factory=list)
    tokens_entree: int = 0
    tokens_sortie: int = 0
    appels_llm: int = 0
    debut: float = field(default_factory=time.perf_counter)

    def ajouter(self, **etape) -> None:
        etape["n"] = len(self.etapes) + 1
        self.etapes.append(etape)

    def compter(self, usage: dict | None) -> None:
        usage = usage or {}
        self.appels_llm += 1
        self.tokens_entree += usage.get("prompt_tokens", 0) or 0
        self.tokens_sortie += usage.get("completion_tokens", 0) or 0

    @property
    def outils_appeles(self) -> list[str]:
        return [e["outil"] for e in self.etapes if e["type"] == "outil"]

    def cout_usd(self, prix_entree_1m: float, prix_sortie_1m: float) -> float:
        return (self.tokens_entree * prix_entree_1m + self.tokens_sortie * prix_sortie_1m) / 1_000_000

    def resume(self) -> dict:
        return {"duree_s": round(time.perf_counter() - self.debut, 2), "nb_etapes": len(self.etapes),
                "appels_llm": self.appels_llm, "outils": self.outils_appeles,
                "tokens_entree": self.tokens_entree, "tokens_sortie": self.tokens_sortie}

    def afficher(self) -> str:
        lignes = ["─" * 70, "TRACE"]
        for e in self.etapes:
            qui = f"[{e['agent']}] " if e.get("agent") else ""
            if e["type"] == "outil":
                res = json.dumps(e["resultat"], ensure_ascii=False)
                res = res if len(res) <= 220 else res[:217] + "..."
                lignes.append(f"[{e['n']}] {qui}🔧 {e['outil']}({json.dumps(e['arguments'], ensure_ascii=False)})"
                              f"  ⏱ {e['duree_ms']} ms\n      → {res}")
            elif e["type"] == "delegation":
                cr = (e.get("compte_rendu") or "").replace("\n", " ")
                lignes.append(f"[{e['n']}] {qui}📨 → {e['vers']} : {e['consigne'][:160]}"
                              f"\n      ← {cr[:200]}")
            elif e["type"] == "evaluation":
                lignes.append(f"[{e['n']}] {qui}🔎 {e['verdict']} {e.get('detail', '')}")
            else:
                lignes.append(f"[{e['n']}] {qui}💬 {e['type']} {e.get('detail', '')}")
        r = self.resume()
        lignes.append(f"Total : {r['appels_llm']} appels LLM · {r['duree_s']} s · "
                      f"{r['tokens_entree']}+{r['tokens_sortie']} tokens · outils={r['outils']}")
        lignes.append("─" * 70)
        return "\n".join(lignes)

    def sauvegarder(self, dossier: str | Path = "traces", question: str = "", reponse: str = "",
                    prefixe: str = "trace") -> Path:
        d = Path(dossier)
        d.mkdir(exist_ok=True)
        path = d / f"{prefixe}_{time.strftime('%Y%m%d_%H%M%S')}.json"
        path.write_text(json.dumps({"question": question, "reponse": reponse, "resume": self.resume(),
                                    "etapes": self.etapes}, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


@dataclass
class ReponseAgent:
    texte: str
    trace: Trace

    @property
    def memo(self) -> str:
        """Note interne : les produits vérifiés pendant ce message (identifiants, quantités, zone).
        Gardée dans l'historique pour le LLM, jamais montrée au client."""
        produits, zone, total = {}, None, None
        for e in self.trace.etapes:
            if e["type"] != "outil" or "erreur" in e["resultat"]:
                continue
            r = e["resultat"]
            if e["outil"] == "verifier_stock":
                produits.setdefault(r["produit_id"], f"{r['produit_id']} = {r['nom']}")
            elif e["outil"] == "frais_livraison" or (e["outil"] == "creer_commande" and "lignes" in r):
                zone = r["zone"]
                total = r.get("total_xof", total)
                for l in r.get("lignes", []):
                    produits[l["produit_id"]] = (f"{l['produit_id']} = {l['nom']} × {l['quantite']} "
                                                 f"({l['prix_unitaire_xof']} FCFA l'unité)")
            if e["outil"] == "creer_commande" and "commande_id" in r:
                produits["_cmd"] = f"commande {r['commande_id']} : {r['statut']}"
        if not produits and not zone:
            return ""
        lignes = [v for k, v in produits.items() if k != "_cmd"]
        txt = "Produits du récapitulatif : " + " ; ".join(lignes) if lignes else ""
        if zone:
            txt += f" · zone de livraison : {zone}"
        if total is not None:
            txt += f" · total annoncé : {total} FCFA"
        if "_cmd" in produits:
            txt += f" · {produits['_cmd']}"
        return txt.strip(" ·")

    def message_historique(self) -> dict:
        """Entrée à ajouter à l'historique de conversation après cette réponse."""
        m = {"role": "assistant", "content": self.texte}
        if self.memo:
            m["memo"] = self.memo
        return m


def pour_le_llm(historique: list[dict]) -> list[dict]:
    """Convertit l'historique pour l'API : la note interne est ajoutée au texte de la vendeuse
    (le client ne la voit jamais : seul `texte` lui est envoyé)."""
    sortie = []
    for m in historique:
        if m.get("memo"):
            sortie.append({"role": m["role"], "content": f"{m['content']}\n\n[Note interne, invisible pour "
                                                          f"le client — {m['memo']}]"})
        else:
            sortie.append({"role": m["role"], "content": m["content"]})
    return sortie


# ---------------------------------------------------------------- boucle générique
async def boucle_outils(llm, messages: list[dict], schemas: list[dict], executer, budget: int,
                        trace: Trace, agent: str, resultats: list[dict]) -> str | None:
    """Fait tourner LLM ↔ outils. Renvoie le texte final, ou None si le budget d'étapes est épuisé.
    `messages` est complété sur place ; `resultats` reçoit {"outil", "resultat"} pour l'évaluation."""
    for _ in range(max(0, budget)):
        rep = await llm.chat(messages, tools=schemas or None)
        trace.compter(rep.usage)
        msg = rep.message
        appels = msg.get("tool_calls") or []
        if not appels:
            return (msg.get("content") or "").strip()

        messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": appels})
        for appel in appels:
            nom = appel["function"]["name"]
            brut = appel["function"].get("arguments") or "{}"
            t0 = time.perf_counter()
            resultat = await executer(nom, brut)
            try:
                args = json.loads(brut) if isinstance(brut, str) else brut
            except json.JSONDecodeError:
                args = brut
            trace.ajouter(type="outil", agent=agent, outil=nom, arguments=args, resultat=resultat,
                          duree_ms=round((time.perf_counter() - t0) * 1000))
            resultats.append({"outil": nom, "resultat": resultat})
            messages.append({"role": "tool", "tool_call_id": appel["id"],
                             "content": json.dumps(resultat, ensure_ascii=False)})
    return None


def textes_du_client(historique: list[dict]) -> list[str]:
    return [m["content"] for m in historique if m.get("role") == "user" and isinstance(m.get("content"), str)]


# ---------------------------------------------------------------- agent seul
class AgentVendeur:
    def __init__(self, llm, toolbox: Toolbox, max_steps: int = 8, system_prompt: str = SYSTEM_VENDEUR,
                 juge=None, seuil_juge: int = 3, max_corrections: int = 2):
        self.llm = llm
        self.tools = toolbox
        self.max_steps = max_steps
        self.system_prompt = system_prompt
        self.juge = juge            # JugeLLM ou None
        self.seuil_juge = seuil_juge
        self.max_corrections = max_corrections  # corrections après un échec des règles en code

    async def repondre(self, historique: list[dict]) -> ReponseAgent:
        """historique = messages user/assistant (sans le system), le dernier étant celui du client."""
        self.tools.nouveau_tour()
        system = self.system_prompt
        if getattr(self.tools, "contexte", {}).get("telephone"):  # WhatsApp : le numéro est déjà connu
            system += NUMERO_CONNU.format(telephone=self.tools.contexte["telephone"])
        messages = [{"role": "system", "content": system}, *pour_le_llm(historique)]
        trace, resultats = Trace(), []
        client = textes_du_client(historique)
        recap = total_du_recap(historique)
        if client and _CONFIRMATION.search(client[-1]):  # « oui » ⇒ creer_commande n'accepte que ces articles
            self.tools.recap_confirme = articles_du_recap(historique) or None
        corrections, reecriture_faite = 0, False
        dernier_bon: str | None = None
        appels_juge = 0  # le juge ne consomme pas le budget d'étapes de l'agent (il reste compté dans le coût)

        while True:
            budget = self.max_steps - (trace.appels_llm - appels_juge)
            texte = await boucle_outils(self.llm, messages, SCHEMAS, self.tools.run, budget,
                                        trace, "vendeuse", resultats) if budget > 0 else None
            if texte is None:  # limite d'étapes atteinte
                trace.ajouter(type="limite_atteinte", detail=f"max_steps={self.max_steps}")
                return ReponseAgent(dernier_bon or MESSAGE_LIMITE, trace)

            # 1) règles en code
            ev = evaluer_regles(texte, resultats, client, recap)
            trace.ajouter(type="evaluation", agent="règles", verdict="✅ règles OK" if ev["ok"] else "❌ règles",
                          detail=" | ".join(ev["problemes"]))
            if not ev["ok"]:
                if corrections >= self.max_corrections:
                    trace.ajouter(type="reponse_finale", detail="message de vérification (échec après correction)")
                    return ReponseAgent(dernier_bon or MESSAGE_VERIFICATION, trace)
                corrections += 1
                messages += [{"role": "assistant", "content": texte},
                             {"role": "user", "content": CORRECTION.format(problemes="\n".join(ev["problemes"]))}]
                continue
            dernier_bon = texte

            # 2) LLM juge (une seule réécriture possible)
            if self.juge is not None and not reecriture_faite:
                avis = await self.juge.noter(historique, texte, resultats)
                trace.compter(avis.get("usage"))
                appels_juge += 1
                trace.ajouter(type="evaluation", agent="juge",
                              verdict=f"⚖️ {avis.get('verdict', 'ok')} (note {avis['note']}/5)",
                              detail=avis.get("commentaire", ""))
                if avis.get("verdict") == "pas encore":  # la critique est ajoutée au contexte, l'agent recommence
                    reecriture_faite = True
                    dernier_bon = None  # une réponse rejetée par le juge n'est jamais envoyée
                    messages += [{"role": "assistant", "content": texte},
                                 {"role": "user", "content": REECRITURE_JUGE.format(**avis)}]
                    continue

            texte = formater_whatsapp(texte)
            trace.ajouter(type="reponse_finale", detail=f"{len(texte)} caractères")
            return ReponseAgent(texte, trace)
