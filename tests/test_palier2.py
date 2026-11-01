"""Tests du palier 2 : validation gérante, persistance du stock, évaluation, équipe."""
import json

from boutique.agent import AgentVendeur
from boutique.equipe import EquipeBoutique
from boutique.evaluation import JugeLLM, regle_total
from boutique.llm import LLMResponse
from boutique.tools import Toolbox
from tests.test_palier1 import FakeLLM, appel, fx_transport, run, store  # noqa: F401 (fixture)


def texte(t):
    return LLMResponse({"role": "assistant", "content": t}, {"prompt_tokens": 10, "completion_tokens": 5})


def outil(i, nom, args):
    return LLMResponse({"role": "assistant", "content": None, "tool_calls": [appel(i, nom, args)]},
                       {"prompt_tokens": 10, "completion_tokens": 5})


async def oui(_):
    return True, ""


async def non(_):
    return False, "plus de pièce en boutique"


def commander(store, validateur, pid="P001", qte=2):
    tb = Toolbox(store, validateur=validateur)
    run(tb.verifier_stock(pid, qte))
    return run(tb.creer_commande("Ada", "+229 97", "cotonou", [{"produit_id": pid, "quantite": qte}], True))


def stock(store, pid):
    cat = run(store.get_catalogue())
    return next(p["stock"] for p in cat["produits"] if p["id"] == pid)


# ------------------------------------------------------------------ validation humaine + persistance
def test_gerante_valide_et_stock_baisse(store):
    r = commander(store, oui)
    assert r["statut"] == "validee"
    assert stock(store, "P001") == 6  # 8 - 2, enregistré dans le fichier


def test_gerante_refuse_et_stock_intact(store):
    r = commander(store, non)
    assert r["statut"] == "refusee" and r["motif"] == "plus de pièce en boutique"
    assert stock(store, "P001") == 8


def test_sans_validateur_reste_en_attente(store):
    r = commander(store, None, qte=1)
    assert r["statut"] == "en_attente_validation_gerante"
    assert stock(store, "P001") == 8  # le stock ne bouge qu'après la gérante


# ------------------------------------------------------------------ règle du total
DEVIS = {"outil": "calculer_livraison", "resultat": {
    "lignes": [{"prix_unitaire_xof": 25000, "quantite": 2, "montant_xof": 50000}],
    "sous_total_articles_xof": 50000, "frais_livraison_xof": 1500, "total_xof": 51500}}


def test_regle_total():
    assert regle_total("*Total : 51 500 FCFA*", [DEVIS])["ok"]
    assert not regle_total("*Total : 50 000 FCFA*", [DEVIS])["ok"]          # oubli de la livraison
    assert not regle_total("Total : 30 000 FCFA", [])["ok"]                  # total sans outil
    assert regle_total("Le Woodin coûte 15 000 FCFA", [])["ok"]               # pas de total annoncé


# ------------------------------------------------------------------ LLM juge
def test_juge_declenche_une_reecriture(store):
    script = [
        outil(1, "chercher_produit", {"requete": "woodin"}),
        texte("woodin 15000"),                                                    # réponse sèche
        texte('{"note": 2, "respecte_regles": true, "commentaire": "trop sec"}'),  # juge
        texte("Bonjour 😊 Le *Pagne Woodin « Soleil »* est à *15 000 FCFA*. Je vous le réserve ?"),
    ]
    llm = FakeLLM(script)
    rep = run(AgentVendeur(llm, Toolbox(store), juge=JugeLLM(llm)).repondre(
        [{"role": "user", "content": "prix woodin ?"}]))
    assert rep.texte.startswith("Bonjour")
    assert any(e.get("agent") == "juge" and "2/5" in e["verdict"] for e in rep.trace.etapes)


# ------------------------------------------------------------------ équipe
def deleguer(i, qui, consigne):
    return outil(i, f"deleguer_{qui}", {"consigne": consigne})


def test_equipe_gerant_delegue(store):
    script = [
        deleguer(1, "vendeur", "Trouve le Vlisco bleu et vérifie 2 pièces"),
        outil(2, "chercher_produit", {"requete": "vlisco bleu"}),
        outil(3, "verifier_stock", {"produit_id": "P001", "quantite": 2}),
        texte("P001 à 25000, 8 en stock"),
        deleguer(4, "logisticien", "Livraison de 2 x P001 à Abomey-Calavi, total en CAD"),
        outil(5, "calculer_livraison", {"zone": "abomey-calavi", "articles": [{"produit_id": "P001", "quantite": 2}]}),
        outil(6, "convertir_devise", {"montant": 51500, "devise_cible": "CAD"}),
        texte("Total 51500 XOF soit 117.94 CAD"),
        deleguer(7, "redacteur", "Donne le total et propose de valider"),
        texte("Oui, en stock ✅ *Total : 51 500 FCFA* (≈ 117,94 $ CA). Je valide ?"),
    ]
    eq = EquipeBoutique(FakeLLM(script), Toolbox(store, fx_transport()))
    rep = run(eq.repondre([{"role": "user", "content": "2 vlisco bleu à Calavi, total en CAD ?"}]))
    assert rep.trace.outils_appeles == ["chercher_produit", "verifier_stock", "calculer_livraison", "convertir_devise"]
    assert [e["vers"] for e in rep.trace.etapes if e["type"] == "delegation"] == ["vendeur", "logisticien", "redacteur"]
    assert "51 500" in rep.texte
    assert rep.trace.appels_llm == 10


def test_equipe_membre_limite_a_ses_outils(store):
    script = [
        deleguer(1, "vendeur", "calcule la livraison"),
        outil(2, "calculer_livraison", {"zone": "cotonou", "articles": []}),  # interdit au vendeur
        texte("je ne peux pas"),
        deleguer(3, "redacteur", "dis bonjour"),
        texte("Bonjour 😊 Que puis-je faire pour vous ?"),
    ]
    rep = run(EquipeBoutique(FakeLLM(script), Toolbox(store)).repondre([{"role": "user", "content": "?"}]))
    erreur = next(e for e in rep.trace.etapes if e["type"] == "outil")["resultat"]
    assert "pas autorisé" in erreur["erreur"]


def test_format_whatsapp_corrige_le_markdown():
    from boutique.evaluation import formater_whatsapp
    brut = "## Récap\n• **Total** : 51 500 FCFA  \n\n\n\nJe valide ?"
    assert formater_whatsapp(brut) == "Récap\n• *Total* : 51 500 FCFA\n\nJe valide ?"


def test_note_interne_garde_les_identifiants_entre_deux_messages(store):
    """Correctif du 31/10 : au 2e message, l'agent retrouve P004 grâce à la note interne."""
    tb = Toolbox(store, validateur=oui)
    tour1 = [outil(1, "verifier_stock", {"produit_id": "P004", "quantite": 1}),
             outil(2, "calculer_livraison", {"zone": "cotonou", "articles": [{"produit_id": "P004", "quantite": 1}]}),
             texte("Pagne Woodin « Soleil » · *Total : 16 000 FCFA*. Je valide ?")]
    llm = FakeLLM(tour1)
    agent = AgentVendeur(llm, tb)
    hist = [{"role": "user", "content": "1 Woodin Soleil à Cotonou"}]
    rep = run(agent.repondre(hist))
    hist.append(rep.message_historique())
    assert "P004" in hist[-1]["memo"] and "P004" not in rep.texte      # le client ne voit pas la note

    llm.script = [outil(3, "verifier_stock", {"produit_id": "P004", "quantite": 1}),
                  outil(4, "creer_commande", {"client_nom": "Amévi", "client_telephone": "70 56 66 86",
                                              "zone": "cotonou", "articles": [{"produit_id": "P004", "quantite": 1}],
                                              "confirmation_client": True}),
                  texte("✅ Commande validée : *Total : 16 000 FCFA*.")]
    hist.append({"role": "user", "content": "Oui je valide, Amévi, 70 56 66 86"})
    rep2 = run(agent.repondre(hist))
    envoye = llm.recu[-3]                                           # messages vus par le LLM au tour 2
    assert any("Note interne" in (m.get("content") or "") and "P004" in m["content"] for m in envoye)
    assert all("memo" not in m for m in envoye)                     # champ inconnu jamais envoyé à l'API
    assert run(store.list_orders())[0]["statut"] == "validee"
    assert "commande CMD-" in rep2.memo
