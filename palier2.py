"""Palier 2 : agent fiabilisé, équipe multi-agents et comparaison.

Usage :
    python palier2.py                 # discuter avec l'agent seul (la gérante valide dans le terminal)
    python palier2.py --equipe        # discuter avec l'équipe Gérant → Vendeur / Logisticien / Rédacteur
    python palier2.py --comparer      # comparer agent seul vs équipe → resultats/comparaison.md
    python palier2.py --etat          # afficher le stock et le fichier de commandes
    python palier2.py --reset         # remettre le stock initial et vider les commandes
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

from boutique.agent import AgentVendeur, ReponseAgent
from boutique.commandes import recap_gerante
from boutique.config import Settings
from boutique.equipe import EquipeBoutique
from boutique.evaluation import JugeLLM, valeurs
from boutique.llm import LLMError, RodiumClient
from boutique.store import JsonFileStore
from boutique.tools import Toolbox

DATA = Path("data")


# ------------------------------------------------------------------ validation humaine (terminal)
async def validation_terminal(commande: dict) -> tuple[bool, str]:
    print("\n" + "═" * 70)
    print("👩🏾‍💼 VALIDATION D'AFI REQUISE\n")
    print(recap_gerante(commande).replace("Répondez", "(Sur WhatsApp, Afi répondrait"))
    print("═" * 70)
    choix = (await asyncio.to_thread(input, "Valider la commande ? [o/N] ")).strip().lower()
    if choix.startswith("o"):  # par défaut (Entrée seule) : NON
        return True, ""
    motif = (await asyncio.to_thread(input, "Motif du refus (facultatif) : ")).strip()
    return False, motif or "refusée par Afi"


def construire(settings: Settings, store: JsonFileStore, equipe: bool, validateur=None):
    llm = RodiumClient(settings.api_key, settings.base_url, settings.model)
    juge = JugeLLM(llm, settings.juge_model) if settings.juge_actif else None
    tools = Toolbox(store, validateur=validateur)
    if equipe:
        return EquipeBoutique(llm, tools, juge=juge)
    return AgentVendeur(llm, tools, max_steps=settings.max_steps, juge=juge)


def resultats_de(rep: ReponseAgent) -> list[dict]:
    return [{"outil": e["outil"], "resultat": e["resultat"]} for e in rep.trace.etapes if e["type"] == "outil"]


# ------------------------------------------------------------------ conversation
async def discuter(equipe: bool) -> None:
    s = Settings.from_env()
    systeme = construire(s, JsonFileStore(DATA), equipe, validateur=validation_terminal)
    nom = "ÉQUIPE" if equipe else "AGENT SEUL"
    print(f"Boutique d'Afi · {nom} · juge {'activé' if s.juge_actif else 'désactivé'}"
          " — tape 'quit' pour sortir.\n")
    historique: list[dict] = []
    while True:
        texte = (await asyncio.to_thread(input, "👤 Vous : ")).strip()
        if texte.lower() in {"quit", "exit", "q"}:
            break
        if not texte:
            continue
        historique.append({"role": "user", "content": texte})
        rep = await systeme.repondre(historique)
        historique.append(rep.message_historique())
        print(rep.trace.afficher())
        cout = rep.trace.cout_usd(s.prix_entree_1m, s.prix_sortie_1m)
        print(f"💰 coût estimé : {cout * s.rodi_par_usd:.2f} RODI ({cout:.5f} $)")
        print(f"\n🛍️  Awa : {rep.texte}\n")
        rep.trace.sauvegarder("traces", texte, rep.texte, prefixe="equipe" if equipe else "seul")


# ------------------------------------------------------------------ comparaison (palier 2)
# Les 3 mêmes demandes pour les deux versions, avec ce qu'on attend (sujet).
DEMANDES = [
    {"nom": "Pagnes en euros (palier 1, test 1)",
     "message": "Vous avez des pagnes ? Combien ça fait en euros pour ma sœur à Paris ?",
     "attendu": "chercher_produits puis convertir_devise ; prix 12 000 FCFA et sa conversion en EUR"},
    {"nom": "Scénario Afi (palier 2a)",
     "message": "Je m'appelle Afi. Je veux 2 robes en wax et 1 sac en raphia, livrés à Calavi.",
     "attendu": "rupture du sac signalée, alternative proposée, total 46 000 FCFA, demande de confirmation"},
    {"nom": "Scénario Koffi (palier 2b)",
     "message": "Je suis Koffi à Porto-Novo. Combien me coûtent 3 pots de beurre de karité livrés, "
                "et ça fait combien en nairas ?",
     "attendu": "total 13 500 FCFA (3 × 3 500 + 3 000) converti en NGN au taux du jour"},
]


def _contient(texte: str, valeur: float, tolerance: float = 0.006) -> bool:
    """Le texte contient-il ce montant (à l'arrondi près : 33,54 € ou ≈ 34 €) ?"""
    return any(abs(v - valeur) <= max(1.0, valeur * tolerance) for v in valeurs(texte))


def verifier_demande(i: int, texte: str, resultats: list[dict]) -> tuple[bool, str]:
    """Réponse correcte selon le sujet ? Contrôle en code, pas par un LLM."""
    outils = [r["outil"] for r in resultats]
    conv = [r["resultat"] for r in resultats if r["outil"] == "convertir_devise" and "montant_converti" in r["resultat"]]
    bas = texte.lower()
    if i == 0:
        if "chercher_produits" not in outils or "convertir_devise" not in outils:
            return False, f"outils appelés : {outils}"
        if outils.index("chercher_produits") > outils.index("convertir_devise"):
            return False, "convertir_devise appelé avant chercher_produits"
        if not _contient(texte, 12000):
            return False, "prix 12 000 FCFA absent"
        if not conv or not any(_contient(texte, c["montant_converti"]) for c in conv if c["devise_cible"] == "EUR"):
            return False, "montant en euros absent ou différent de l'outil"
        return True, "outils dans l'ordre, prix et conversion exacts"
    if i == 1:
        if "creer_commande" in outils:
            return False, "commande créée sans confirmation"
        if not _contient(texte, 46000):
            return False, "total 46 000 FCFA absent"
        if "raphia" not in bas or not any(m in bas for m in ("rupture", "plus en stock", "indisponible",
                                                              "épuisé", "plus disponible", "n'est pas disponible",
                                                              "n'avons plus", "pas en stock")):
            return False, "rupture du sac non signalée"
        alternatives = set()
        for r in resultats:  # alternatives renvoyées par n'importe quel outil
            res = r["resultat"]
            for l in [res] + res.get("resultats", []) + res.get("articles_non_disponibles", []):
                alternatives |= {a["nom"].lower() for a in l.get("alternatives", [])}
        if not any(a.split()[0] in bas for a in alternatives):
            return False, "aucune alternative proposée"
        if "?" not in texte:
            return False, "pas de demande de confirmation"
        return True, "rupture, alternative, total 46 000 FCFA et confirmation"
    if not _contient(texte, 13500):
        return False, "total 13 500 FCFA absent"
    bons = [c for c in conv if c["devise_cible"] == "NGN" and abs(c["montant_source"] - 13500) < 1]
    if not bons or not any(_contient(texte, c["montant_converti"]) for c in bons):
        return False, "conversion de 13 500 FCFA en NGN absente ou inexacte"
    return True, "total 13 500 FCFA et conversion NGN exacts"


async def gerante_accepte(_commande):
    return True, ""


async def executer_demande(s: Settings, equipe: bool, i: int, demande: dict) -> dict:
    with tempfile.TemporaryDirectory() as tmp:  # données neuves à chaque demande
        shutil.copy(DATA / "catalogue_initial.json", Path(tmp) / "catalogue.json")
        shutil.copy(DATA / "livraison.json", Path(tmp) / "livraison.json")
        systeme = construire(s, JsonFileStore(tmp), equipe, validateur=gerante_accepte)
        t0 = time.perf_counter()
        rep = await systeme.repondre([{"role": "user", "content": demande["message"]}])
        duree = time.perf_counter() - t0
    resultats = resultats_de(rep)
    correct, raison = verifier_demande(i, rep.texte, resultats)
    r = rep.trace.resume()
    nom = "equipe" if equipe else "seul"
    Path("resultats/traces").mkdir(parents=True, exist_ok=True)
    rep.trace.sauvegarder("resultats/traces", demande["message"], rep.texte, prefixe=f"{nom}_demande{i + 1}")
    cout = rep.trace.cout_usd(s.prix_entree_1m, s.prix_sortie_1m)
    return {"systeme": nom, "demande": demande["nom"], "reponse": rep.texte, "correct": correct, "raison": raison,
            "duree_s": round(duree, 2), "appels_llm": r["appels_llm"],
            "tokens": r["tokens_entree"] + r["tokens_sortie"], "cout_usd": cout, "cout_rodi": cout * s.rodi_par_usd,
            "outils": r["outils"]}


async def comparer() -> None:
    s = Settings.from_env()
    lignes = []
    for i, d in enumerate(DEMANDES):
        for equipe in (False, True):
            print(f"▶ {d['nom']:<36} {'multi-agents' if equipe else 'agent seul':<12} ...", end=" ", flush=True)
            res = await executer_demande(s, equipe, i, d)
            print(f"{'✅' if res['correct'] else '❌'} {res['duree_s']} s · {res['appels_llm']} appels · "
                  f"{res['cout_rodi']:.2f} RODI · {res['raison']}")
            lignes.append(res)

    def stats(nom):
        g = [l for l in lignes if l["systeme"] == nom]
        return {"correct": sum(l["correct"] for l in g), "appels": sum(l["appels_llm"] for l in g),
                "appels_moy": statistics.mean(l["appels_llm"] for l in g),
                "duree": statistics.mean(l["duree_s"] for l in g),
                "rodi": sum(l["cout_rodi"] for l in g), "usd": sum(l["cout_usd"] for l in g)}

    a, b = stats("seul"), stats("equipe")

    def x(n, d):  # rapport « n / d » sans division par zéro
        return n / d if d else float("nan")

    faits = (f"Sur ces 3 demandes, l'agent seul a donné {a['correct']}/3 réponses correctes contre {b['correct']}/3 "
             f"pour le multi-agents, avec {a['appels']} appels LLM contre {b['appels']}, {a['duree']:.1f} s contre "
             f"{b['duree']:.1f} s en moyenne et {a['rodi']:.2f} RODI contre {b['rodi']:.2f}.")
    if a["correct"] >= b["correct"] and a["appels"] <= b["appels"]:
        conclusion = (f"{faits} Le multi-agents ne valait donc pas le coup ici : il multiplie les étapes, donc les "
                      "occasions d'erreur (95 % de réussite par étape sur 10 étapes ≈ 60 % de bout en bout), sans "
                      "gain de justesse, alors qu'un seul agent avec 5 outils suffit pour cette boutique.")
    elif b["correct"] > a["correct"]:
        conclusion = (f"{faits} Le multi-agents a été plus juste, mais au prix de plus d'étapes : il ne vaudrait le "
                      "coup que si ce gain se confirme sur davantage de demandes (95 % sur 10 étapes ≈ 60 %).")
    else:
        conclusion = (f"{faits} Les deux versions sont aussi justes ; le choix se fait donc sur la simplicité : moins "
                      "d'étapes, c'est moins d'occasions d'erreur (95 % sur 10 étapes ≈ 60 %).")

    md = ["# Comparaison agent seul vs multi-agents\n",
          f"Modèle : `{s.model}` · juge : `{s.juge_model}` (évaluation {'activée' if s.juge_actif else 'désactivée'}) · "
          f"coût : {s.prix_entree_1m} $ / {s.prix_sortie_1m} $ par million de tokens, {s.rodi_par_usd} RODI par $\n",
          "## Tableau (3 mêmes demandes)\n",
          "| | Agent seul | Multi-agents |", "|---|---|---|",
          f"| Réponses correctes (/3) | {a['correct']}/3 | {b['correct']}/3 |",
          f"| Nombre d'appels LLM (total · moyenne par demande) | {a['appels']} · {a['appels_moy']:.1f} | "
          f"{b['appels']} · {b['appels_moy']:.1f} |",
          f"| Temps de réponse moyen | {a['duree']:.1f} s | {b['duree']:.1f} s |",
          f"| Coût estimé (RODI, total des 3 demandes) | {a['rodi']:.2f} RODI | {b['rodi']:.2f} RODI |",
          f"\n**Le multi-agents valait-il le coup ici ?** {conclusion}\n",
          "## Détail par demande\n",
          "| Demande | Version | Correct | Pourquoi | Temps | Appels LLM | Coût | Outils |",
          "|---|---|---|---|---|---|---|---|"]
    for l in lignes:
        md.append(f"| {l['demande']} | {'multi-agents' if l['systeme'] == 'equipe' else 'agent seul'} | "
                  f"{'✅' if l['correct'] else '❌'} | {l['raison']} | {l['duree_s']} s | {l['appels_llm']} | "
                  f"{l['cout_rodi']:.2f} RODI | {', '.join(l['outils'])} |")
    md.append("\n## Demandes et réponses\n")
    for i, d in enumerate(DEMANDES):
        md.append(f"### {d['nom']}\n\n> 👤 {d['message']}\n\n*Attendu : {d['attendu']}*\n")
        for l in [l for l in lignes if l["demande"] == d["nom"]]:
            md.append(f"**{'Multi-agents' if l['systeme'] == 'equipe' else 'Agent seul'}** "
                      f"({'✅ correct' if l['correct'] else '❌ ' + l['raison']})\n\n> "
                      + l["reponse"].replace("\n", "\n> ") + "\n")

    Path("resultats").mkdir(exist_ok=True)
    Path("resultats/comparaison.md").write_text("\n".join(md), encoding="utf-8")
    Path("resultats/comparaison.json").write_text(json.dumps(lignes, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "\n".join(md[3:10]))
    print("\n📄 Rapport complet : resultats/comparaison.md")


async def etat() -> None:
    store = JsonFileStore(DATA)
    print("📦 STOCK")
    for p in (await store.get_catalogue())["produits"]:
        print(f"  {p['id']}  {p['nom']:<26} {p['prix']:>7} FCFA   stock : {p['stock']}")
    commandes = await store.list_orders()
    print(f"\n🧾 COMMANDES ({len(commandes)}) — fichier data/commandes.json")
    for c in commandes:
        articles = ", ".join(f"{l['quantite']} × {l['nom']}" for l in c["lignes"])
        print(f"  #{c['id']}  {c['statut']:<30} {articles} · {c['zone']} · {c['total_xof']} FCFA"
              + (f" · motif : {c['motif']}" if c.get("motif") else ""))


def reset() -> None:
    shutil.copy(DATA / "catalogue_initial.json", DATA / "catalogue.json")
    for f in ("commandes.json", "etat.json"):
        (DATA / f).unlink(missing_ok=True)
    print("✅ Stock initial restauré, commandes et historiques effacés.")


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--equipe", action="store_true")
    p.add_argument("--comparer", action="store_true")
    p.add_argument("--reset", action="store_true")
    p.add_argument("--etat", action="store_true")
    a = p.parse_args()
    try:
        if a.reset:
            reset()
        elif a.etat:
            await etat()
        elif a.comparer:
            await comparer()
        else:
            await discuter(a.equipe)
    except LLMError as e:
        print(f"❌ {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
