"""Tests du palier 2 : validation par Afi, persistance du stock, évaluation, équipe, comparaison."""
from boutique.agent import AgentVendeur
from boutique.equipe import EquipeBoutique
from boutique.evaluation import JugeLLM, formater_whatsapp, regle_total
from boutique.llm import LLMResponse
from boutique.tools import Toolbox
from palier2 import verifier_demande
from tests.test_palier1 import FakeLLM, appel, fx_transport, run, store  # noqa: F401 (fixture)


def texte(t):
    return LLMResponse({"role": "assistant", "content": t}, {"prompt_tokens": 10, "completion_tokens": 5})


def outil(i, nom, args):
    return LLMResponse({"role": "assistant", "content": None, "tool_calls": [appel(i, nom, args)]},
                       {"prompt_tokens": 10, "completion_tokens": 5})


async def oui(_):
    return True, ""


async def non(_):
    return False, "refusée par Afi"


def commander(store, validateur, pid="P05", qte=2, zone="Calavi"):
    tb = Toolbox(store, validateur=validateur)
    run(tb.verifier_stock(pid, qte))
    return run(tb.creer_commande("Afi", zone, [{"produit_id": pid, "quantite": qte}], True))


def stock(store, pid):
    return next(p["stock"] for p in run(store.get_catalogue())["produits"] if p["id"] == pid)


# ------------------------------------------------------------------ 2a · validation humaine + persistance
def test_afi_refuse_puis_accepte(store):
    """Test du sujet : refuser une première fois, puis accepter ; vérifier commandes et stock."""
    r1 = commander(store, non)
    assert r1["statut"] == "refusee" and "REFUSÉE" in r1["message"]   # le refus revient à l'agent
    assert stock(store, "P05") == 5                                      # rien n'est décrémenté
    r2 = commander(store, oui)
    assert r2["statut"] == "validee" and r2["total_xof"] == 46000
    assert stock(store, "P05") == 3                                      # 5 - 2, enregistré dans le fichier
    commandes = run(store.list_orders())
    assert [(c["id"], c["statut"]) for c in commandes] == [("1", "refusee"), ("2", "validee")]


def test_sans_validateur_reste_en_attente(store):
    r = commander(store, None, qte=1)
    assert r["statut"] == "en_attente_validation_gerante" and stock(store, "P05") == 5


# ------------------------------------------------------------------ 2a · évaluation
DEVIS = {"outil": "frais_livraison", "resultat": {
    "zone": "Calavi", "lignes": [{"prix_unitaire_xof": 22000, "quantite": 2, "montant_xof": 44000}],
    "sous_total_articles_xof": 44000, "frais_livraison_xof": 2000, "total_xof": 46000}}


def test_regle_total():
    assert regle_total("*Total : 46 000 FCFA*", [DEVIS])["ok"]
    assert not regle_total("*Total : 44 000 FCFA*", [DEVIS])["ok"]          # oubli de la livraison
    assert not regle_total("Total : 30 000 FCFA", [])["ok"]                  # total sans outil
    assert regle_total("La robe coûte 22 000 FCFA", [])["ok"]                 # pas de total annoncé


def test_juge_pas_encore_relance_l_agent(store):
    script = [
        outil(1, "chercher_produits", {"requete": "robe"}),
        texte("robe 22000"),                                                       # règles OK, mais sec
        texte('{"verdict": "pas encore", "note": 2, "respecte_regles": true, '
              '"commentaire": "trop sec, aucune étape suivante proposée"}'),        # juge
        texte("Bonjour 😊 La *Robe en wax* est à *22 000 FCFA*. Je vous en réserve une ?"),
    ]
    llm = FakeLLM(script)
    rep = run(AgentVendeur(llm, Toolbox(store), juge=JugeLLM(llm)).repondre(
        [{"role": "user", "content": "C'est combien la robe en wax ?"}]))
    assert rep.texte.startswith("Bonjour")
    juge = [e for e in rep.trace.etapes if e.get("agent") == "juge"]
    assert "pas encore" in juge[0]["verdict"]
    assert any("trop sec" in (m.get("content") or "") and m["role"] == "user"
               for m in llm.recu[-1])                                     # la critique est dans le contexte


def test_format_whatsapp_corrige_le_markdown():
    brut = "## Récap\n• **Total** : 46 000 FCFA  \n\n\n\nJe valide ?"
    assert formater_whatsapp(brut) == "Récap\n• *Total* : 46 000 FCFA\n\nJe valide ?"


# ------------------------------------------------------------------ 2b · équipe
def demander(i, qui, consigne):
    return outil(i, f"demander_au_{qui}", {"consigne": consigne})


def test_equipe_scenario_koffi(store):
    script = [
        demander(1, "vendeur", "Prix et stock de 3 pots de beurre de karité"),
        outil(2, "chercher_produits", {"requete": "beurre de karité"}),
        outil(3, "verifier_stock", {"produit_id": "P06", "quantite": 3}),
        texte("P06 à 3500, 40 en stock"),
        demander(4, "logisticien", "Total de 3 × P06 livrés à Porto-Novo, converti en NGN"),
        outil(5, "frais_livraison", {"zone": "Porto-Novo", "articles": [{"produit_id": "P06", "quantite": 3}]}),
        outil(6, "convertir_devise", {"montant": 13500, "devise_cible": "NGN"}),
        texte("Total 13500 FCFA soit 36585 NGN"),
        demander(7, "redacteur", "Donne le total en FCFA et en nairas"),
        texte("Bonjour Koffi 😊 3 pots de *Beurre de karité* livrés à Porto-Novo : *13 500 FCFA*, "
              "soit ≈ 36 585 NGN. Je vous les réserve ?"),
    ]
    rep = run(EquipeBoutique(FakeLLM(script), Toolbox(store, fx_transport())).repondre(
        [{"role": "user", "content": "Je suis Koffi à Porto-Novo. 3 pots de karité livrés, en nairas ?"}]))
    assert rep.trace.outils_appeles == ["chercher_produits", "verifier_stock", "frais_livraison", "convertir_devise"]
    assert [e["vers"] for e in rep.trace.etapes if e["type"] == "delegation"] == ["vendeur", "logisticien", "redacteur"]
    assert "13 500" in rep.texte and rep.trace.appels_llm == 10


def test_equipe_membre_limite_a_ses_outils(store):
    script = [
        demander(1, "vendeur", "calcule la livraison"),
        outil(2, "frais_livraison", {"zone": "Cotonou"}),           # interdit au vendeur
        texte("je ne peux pas"),
        demander(3, "redacteur", "dis bonjour"),
        texte("Bonjour 😊 Que puis-je faire pour vous ?"),
    ]
    rep = run(EquipeBoutique(FakeLLM(script), Toolbox(store)).repondre([{"role": "user", "content": "?"}]))
    erreur = next(e for e in rep.trace.etapes if e["type"] == "outil")["resultat"]
    assert "pas autorisé" in erreur["erreur"]


# ------------------------------------------------------------------ note interne entre deux messages
def test_note_interne_garde_les_references(store):
    tb = Toolbox(store, validateur=oui)
    llm = FakeLLM([outil(1, "verifier_stock", {"produit_id": "P05", "quantite": 2}),
                   outil(2, "frais_livraison", {"zone": "Calavi", "articles": [{"produit_id": "P05", "quantite": 2}]}),
                   texte("2 *Robes en wax* livrées à Calavi : *Total : 46 000 FCFA*. Je valide ?")])
    agent = AgentVendeur(llm, tb)
    hist = [{"role": "user", "content": "2 robes en wax à Calavi, je m'appelle Afi"}]
    rep = run(agent.repondre(hist))
    hist.append(rep.message_historique())
    assert "P05" in hist[-1]["memo"] and "P05" not in rep.texte        # le client ne voit pas la note

    llm.script = [outil(3, "verifier_stock", {"produit_id": "P05", "quantite": 2}),
                  outil(4, "creer_commande", {"client_nom": "Afi", "zone": "Calavi",
                                              "articles": [{"produit_id": "P05", "quantite": 2}],
                                              "confirmation_client": True}),
                  texte("✅ Commande validée : *Total : 46 000 FCFA*.")]
    hist.append({"role": "user", "content": "Oui je confirme"})
    rep2 = run(agent.repondre(hist))
    envoye = llm.recu[-3]
    assert any("Note interne" in (m.get("content") or "") and "P05" in m["content"] for m in envoye)
    assert all("memo" not in m for m in envoye)                        # champ inconnu jamais envoyé à l'API
    assert run(store.list_orders())[0]["statut"] == "validee" and "commande 1" in rep2.memo


# ------------------------------------------------------------------ correction automatique de la comparaison
def test_verification_des_3_demandes():
    conv_eur = {"outil": "convertir_devise", "resultat": {"montant_source": 12000, "montant_converti": 18.29,
                                                          "devise_cible": "EUR"}}
    res1 = [{"outil": "chercher_produits", "resultat": {}}, conv_eur]
    assert verifier_demande(0, "Pagne à *12 000 FCFA*, soit ≈ 18,29 €.", res1)[0]
    assert not verifier_demande(0, "Pagne à *12 000 FCFA*.", res1)[0]

    alt = {"outil": "verifier_stock", "resultat": {"alternatives": [{"nom": "Sandales en cuir"}]}}
    ok = "Le sac en raphia est en rupture, je vous propose des sandales en cuir. Total : 46 000 FCFA. Je valide ?"
    assert verifier_demande(1, ok, [alt])[0]
    assert not verifier_demande(1, ok.replace("46 000", "53 500"), [alt])[0]

    conv_ngn = {"outil": "convertir_devise", "resultat": {"montant_source": 13500, "montant_converti": 36585.0,
                                                          "devise_cible": "NGN"}}
    assert verifier_demande(2, "Total *13 500 FCFA*, soit ≈ 36 585 NGN.", [conv_ngn])[0]


# ------------------------------------------------------------------ bug réel du 01/11 (scénario d'Afi)
def test_total_afi_exclut_le_sac_en_rupture(store):
    """Le sac en raphia (rupture) avait été compté : 53 500 FCFA au lieu de 46 000."""
    r = run(Toolbox(store).frais_livraison("Calavi", [{"produit_id": "P05", "quantite": 2},
                                                      {"produit_id": "P03", "quantite": 1}]))
    assert r["total_xof"] == 46000 and [l["produit_id"] for l in r["lignes"]] == ["P05"]
    exclu = r["articles_non_disponibles"][0]
    assert exclu["nom"] == "Sac en raphia" and exclu["alternatives"][0]["nom"] == "Sandales en cuir"


def test_regle_rupture_ne_bloque_pas_la_confirmation():
    from boutique.evaluation import evaluer_regles
    res = [{"outil": "verifier_stock", "resultat": {"nom": "Sac en raphia", "prix_xof": 7500, "disponible": False,
                                                    "alternatives": [{"nom": "Sandales en cuir"}]}}]
    assert evaluer_regles("Merci Afi, votre commande est transmise ✅", res, ["oui je confirme"])["ok"]


def test_commande_renvoie_le_detail_pour_le_total(store):
    from boutique.evaluation import evaluer_regles
    tb = Toolbox(store, validateur=oui)
    run(tb.verifier_stock("P05", 2))
    r = run(tb.creer_commande("Afi", "Calavi", [{"produit_id": "P05", "quantite": 2}], True))
    res = [{"outil": "creer_commande", "resultat": r}]
    assert evaluer_regles("✅ Commande validée : 2 robes (44 000 FCFA) + livraison 2 000 FCFA, total 46 000 FCFA.",
                          res, ["oui je confirme"])["ok"]


# ------------------------------------------------------------------ bug réel du 01/11 (« oui » → sandales ajoutées)
def test_oui_confirme_exactement_le_recap(store):
    """Après « oui je confirme », l'agent avait ajouté l'alternative (sandales) : 55 000 au lieu de 46 000."""
    from boutique.evaluation import evaluer_regles, total_du_recap
    tb = Toolbox(store)
    llm = FakeLLM([outil(1, "frais_livraison", {"zone": "Calavi", "articles": [{"produit_id": "P05", "quantite": 2}]}),
                   texte("2 *Robes en wax* à Calavi : *Total : 46 000 FCFA*. Je valide ?")])
    hist = [{"role": "user", "content": "2 robes en wax et un sac en raphia à Calavi"}]
    rep = run(AgentVendeur(llm, tb).repondre(hist))
    hist += [rep.message_historique(), {"role": "user", "content": "oui je confirme"}]
    assert "total annoncé : 46000" in hist[1]["memo"] and total_du_recap(hist) == 46000

    avec_sandales = run(tb.frais_livraison("Calavi", [{"produit_id": "P05", "quantite": 2},
                                                      {"produit_id": "P04", "quantite": 1}]))
    res = [{"outil": "frais_livraison", "resultat": avec_sandales}]
    ev = evaluer_regles("*Total : 55 000 FCFA*. Votre nom ?", res, ["oui je confirme"], 46000)
    assert not ev["ok"] and "N'ajoute pas l'alternative" in ev["problemes"][0]
    # le client demande explicitement les sandales : pas un « oui » simple → autorisé
    assert evaluer_regles("*Total : 55 000 FCFA*.", res, ["je prends aussi les sandales"], 46000)["ok"]


# ------------------------------------------------------------------ bug réel du 01/11 (juge → sandales, budget épuisé)
def test_alternative_non_demandee_hors_total(store):
    from boutique.evaluation import evaluer_regles
    tb = Toolbox(store)
    sac = run(tb.chercher_produits("sac en raphia"))
    avec = run(tb.frais_livraison("Calavi", [{"produit_id": "P05", "quantite": 2}, {"produit_id": "P04", "quantite": 1}]))
    res = [{"outil": "chercher_produits", "resultat": sac}, {"outil": "frais_livraison", "resultat": avec}]
    demande = ["Je m'appelle Afi. Je veux 2 robes en wax et 1 sac en raphia, livrés à Calavi."]
    ev = evaluer_regles("Sac en raphia 7 500 FCFA en rupture, Sandales en cuir 9 000 FCFA. Total : 55 000 FCFA", res, demande)
    assert not ev["ok"] and any("alternative proposée" in p for p in ev["problemes"])


def test_le_juge_ne_consomme_pas_le_budget_de_l_agent(store):
    script = [outil(1, "chercher_produits", {"requete": "robe"}),
              texte("Robe en wax : 22 000 FCFA"),
              texte('{"verdict": "pas encore", "note": 2, "commentaire": "propose une étape suivante"}'),
              texte("La *Robe en wax* est à *22 000 FCFA*. Je vous en réserve une ?")]
    llm = FakeLLM(script)
    rep = run(AgentVendeur(llm, Toolbox(store), max_steps=3, juge=JugeLLM(llm)).repondre(
        [{"role": "user", "content": "La robe en wax ?"}]))
    assert rep.texte.startswith("La *Robe") and rep.trace.appels_llm == 4   # 3 agent + 1 juge


# ------------------------------------------------------------------ bug réel du 01/11 (« oui » → beurre de karité ajouté)
def test_oui_creer_commande_refuse_un_article_hors_recap(store):
    """Après « oui je confirme », la commande #1 contenait 1 beurre de karité jamais demandé (49 500 FCFA)."""
    tb = Toolbox(store, validateur=oui)
    llm = FakeLLM([outil(1, "frais_livraison", {"zone": "Calavi", "articles": [{"produit_id": "P05", "quantite": 2}]}),
                   texte("2 *Robes en wax* à Calavi : *Total : 46 000 FCFA* (hors sac). Je valide ?")])
    agent = AgentVendeur(llm, tb)
    hist = [{"role": "user", "content": "Je m'appelle Afi. 2 robes en wax et 1 sac en raphia à Calavi."}]
    hist.append(run(agent.repondre(hist)).message_historique())
    hist.append({"role": "user", "content": "oui je confirme"})
    faux = [{"produit_id": "P05", "quantite": 2}, {"produit_id": "P06", "quantite": 1}]
    bon = [{"produit_id": "P05", "quantite": 2}]
    llm.script = [outil(2, "verifier_stock", {"produit_id": "P05", "quantite": 2}),
                  outil(3, "verifier_stock", {"produit_id": "P06", "quantite": 1}),
                  outil(4, "creer_commande", {"client_nom": "Afi", "zone": "Calavi", "articles": faux,
                                              "confirmation_client": True}),
                  outil(5, "creer_commande", {"client_nom": "Afi", "zone": "Calavi", "articles": bon,
                                              "confirmation_client": True}),
                  texte("✅ Commande validée : *Total : 46 000 FCFA*.")]
    rep = run(agent.repondre(hist))
    cmd = [e["resultat"] for e in rep.trace.etapes if e.get("outil") == "creer_commande"]
    assert "différents du récapitulatif" in cmd[0]["erreur"] and cmd[1]["total_xof"] == 46000
    assert [c["total_xof"] for c in run(store.list_orders())] == [46000]      # jamais de commande à 49 500


def test_deux_corrections_avant_message_de_secours(store):
    """Comparaison du 01/11 : 2 réponses bloquées → message de secours. On autorise 2 corrections."""
    llm = FakeLLM([outil(1, "chercher_produits", {"requete": "robe"}),
                   texte("Robe : 99 999 FCFA"), texte("Robe : 88 888 FCFA"),          # 2 échecs des règles
                   texte("La *Robe en wax* est à *22 000 FCFA*. Je vous en réserve une ?")])
    rep = run(AgentVendeur(llm, Toolbox(store)).repondre([{"role": "user", "content": "La robe en wax ?"}]))
    assert rep.texte.startswith("La *Robe")


# ------------------------------------------------------------------ bugs réels de la comparaison du 01/11 (équipe)
def test_equipe_logisticien_ne_peut_pas_deviner_une_reference(store):
    """Le logisticien avait deviné R01/S01 puis P01/P02 (pagnes + chemise) → total faux de 44 500 FCFA."""
    script = [demander(1, "logisticien", "Total de 2 robes en wax et 1 sac à Calavi"),
              outil(2, "frais_livraison", {"zone": "Calavi", "articles": [{"produit_id": "P01", "quantite": 2}]}),
              texte("références inconnues"),
              demander(3, "redacteur", "dis que tu vérifies"),
              texte("Bonjour 😊 Je vérifie les articles et je reviens vers vous.")]
    rep = run(EquipeBoutique(FakeLLM(script), Toolbox(store)).repondre([{"role": "user", "content": "2 robes ?"}]))
    res = next(e for e in rep.trace.etapes if e.get("outil") == "frais_livraison")["resultat"]
    assert "jamais renvoyée" in res["erreur"]


def test_equipe_reponse_rejetee_par_le_juge_jamais_envoyee(store):
    script = [demander(1, "vendeur", "prix robe"),
              outil(2, "chercher_produits", {"requete": "robe en wax"}),
              texte("P05 22000"),
              demander(3, "redacteur", "donne le prix"),
              texte("La *Robe en wax* est à 22 000 FCFA."),                                   # règles OK
              texte('{"verdict": "pas encore", "note": 2, "commentaire": "faux"}'),          # juge
              texte("La robe est à 99 999 FCFA.")]                                            # réécriture refusée
    llm = FakeLLM(script)
    rep = run(EquipeBoutique(llm, Toolbox(store), juge=JugeLLM(llm)).repondre(
        [{"role": "user", "content": "La robe en wax ?"}]))
    assert rep.texte.startswith("Je préfère vérifier")
