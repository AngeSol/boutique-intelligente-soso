# Comparaison agent seul vs multi-agents

Modèle : `openai/gpt-4o-mini` · juge : `anthropic/claude-sonnet-4-6` (évaluation activée) · coût : 0.15 $ / 0.6 $ par million de tokens, 757.6 RODI par $

## Tableau (3 mêmes demandes)

| | Agent seul | Multi-agents |
|---|---|---|
| Réponses correctes (/3) | 3/3 | 2/3 |
| Nombre d'appels LLM (total · moyenne par demande) | 16 · 5.3 | 35 · 11.7 |
| Temps de réponse moyen | 10.0 s | 19.5 s |
| Coût estimé (RODI, total des 3 demandes) | 3.81 RODI | 4.04 RODI |

**Le multi-agents valait-il le coup ici ?** Sur ces 3 demandes, l'agent seul a donné 3/3 réponses correctes contre 2/3 pour le multi-agents, avec 16 appels LLM contre 35, 10.0 s contre 19.5 s en moyenne et 3.81 RODI contre 4.04. Le multi-agents ne valait donc pas le coup ici : il multiplie les étapes, donc les occasions d'erreur (95 % de réussite par étape sur 10 étapes ≈ 60 % de bout en bout), sans gain de justesse, alors qu'un seul agent avec 5 outils suffit pour cette boutique.

## Détail par demande

| Demande | Version | Correct | Pourquoi | Temps | Appels LLM | Coût | Outils |
|---|---|---|---|---|---|---|---|
| Pagnes en euros (palier 1, test 1) | agent seul | ✅ | outils dans l'ordre, prix et conversion exacts | 7.94 s | 4 | 0.82 RODI | chercher_produits, convertir_devise |
| Pagnes en euros (palier 1, test 1) | multi-agents | ✅ | outils dans l'ordre, prix et conversion exacts | 15.27 s | 9 | 0.89 RODI | chercher_produits, convertir_devise |
| Scénario Afi (palier 2a) | agent seul | ✅ | rupture, alternative, total 46 000 FCFA et confirmation | 14.16 s | 7 | 1.87 RODI | chercher_produits, chercher_produits, verifier_stock, frais_livraison, frais_livraison, chercher_produits |
| Scénario Afi (palier 2a) | multi-agents | ❌ | total 46 000 FCFA absent | 24.01 s | 13 | 1.75 RODI | chercher_produits, chercher_produits, verifier_stock, frais_livraison, frais_livraison |
| Scénario Koffi (palier 2b) | agent seul | ✅ | total 13 500 FCFA et conversion NGN exacts | 8.03 s | 5 | 1.12 RODI | chercher_produits, frais_livraison, convertir_devise |
| Scénario Koffi (palier 2b) | multi-agents | ✅ | total 13 500 FCFA et conversion NGN exacts | 19.3 s | 13 | 1.41 RODI | chercher_produits, verifier_stock, frais_livraison, convertir_devise |

## Demandes et réponses

### Pagnes en euros (palier 1, test 1)

> 👤 Vous avez des pagnes ? Combien ça fait en euros pour ma sœur à Paris ?

*Attendu : chercher_produits puis convertir_devise ; prix 12 000 FCFA et sa conversion en EUR*

**Agent seul** (✅ correct)

> Nous avons le *Pagne wax 6 yards* à *12 000 FCFA*, soit environ *18,29 €*. Si vous souhaitez en commander un, n'hésitez pas à me le faire savoir !

**Multi-agents** (✅ correct)

> Bonjour ! Nous avons un *pagne wax de 6 yards* disponible au prix de *12 000 FCFA*, soit environ *18,29 €*. Souhaitez-vous passer commande ? Je vous attends !

### Scénario Afi (palier 2a)

> 👤 Je m'appelle Afi. Je veux 2 robes en wax et 1 sac en raphia, livrés à Calavi.

*Attendu : rupture du sac signalée, alternative proposée, total 46 000 FCFA, demande de confirmation*

**Agent seul** (✅ correct)

> Voici votre récapitulatif :
> 
> • *2 Robes en wax* à 22 000 FCFA chacune
> • *Sac en raphia* à 7 500 FCFA (en rupture)
> • *Frais de livraison* à Calavi : 2 000 FCFA
> • *Total* : 46 000 FCFA
> 
> Je peux vous proposer des *Sandales en cuir* à 9 000 FCFA à la place.
> 
> Je valide ?

**Multi-agents** (❌ total 46 000 FCFA absent)

> Je préfère vérifier ces informations avec la gérante avant de vous répondre, pour ne pas vous donner de mauvais chiffres. Je reviens vers vous très vite 🙏

### Scénario Koffi (palier 2b)

> 👤 Je suis Koffi à Porto-Novo. Combien me coûtent 3 pots de beurre de karité livrés, et ça fait combien en nairas ?

*Attendu : total 13 500 FCFA (3 × 3 500 + 3 000) converti en NGN au taux du jour*

**Agent seul** (✅ correct)

> Pour 3 pots de *Beurre de karité 500 g* à 3 500 FCFA chacun, le total est de *13 500 FCFA*, frais de livraison inclus. Cela correspond à environ *30 870 NGN*.
> 
> Le délai de livraison est de 24 à 48 heures. Souhaitez-vous passer commande ?

**Multi-agents** (✅ correct)

> Bonjour Koffi, le coût total pour 3 pots de beurre de karité (500 g) est de *13 500 FCFA*, soit environ *30 869,80 Naira*. La livraison à Porto-Novo se fera dans un délai de 24 à 48 heures. Souhaitez-vous passer commande ?
