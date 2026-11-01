# Comparaison agent seul vs multi-agents

Modèle : `openai/gpt-4o-mini` · juge : `anthropic/claude-sonnet-4-6` (évaluation activée) · coût : 0.15 $ / 0.6 $ par million de tokens, 757.6 RODI par $

## Tableau (3 mêmes demandes)

| | Agent seul | Multi-agents |
|---|---|---|
| Réponses correctes (/3) | 2/3 | 1/3 |
| Nombre d'appels LLM (total · moyenne par demande) | 14 · 4.7 | 32 · 10.7 |
| Temps de réponse moyen | 7.4 s | 18.2 s |
| Coût estimé (RODI, total des 3 demandes) | 3.43 RODI | 4.02 RODI |

**Le multi-agents valait-il le coup ici ?** Sur ces 3 demandes, l'agent seul a donné 2/3 réponses correctes contre 1/3 pour le multi-agents, avec 14 appels LLM contre 32, 7.4 s contre 18.2 s en moyenne et 3.43 RODI contre 4.02. Le multi-agents ne valait donc pas le coup ici : il multiplie les étapes, donc les occasions d'erreur (95 % de réussite par étape sur 10 étapes ≈ 60 % de bout en bout), sans gain de justesse, alors qu'un seul agent avec 5 outils suffit pour cette boutique.

## Détail par demande

| Demande | Version | Correct | Pourquoi | Temps | Appels LLM | Coût | Outils |
|---|---|---|---|---|---|---|---|
| Pagnes en euros (palier 1, test 1) | agent seul | ✅ | outils dans l'ordre, prix et conversion exacts | 6.81 s | 4 | 0.81 RODI | chercher_produits, convertir_devise |
| Pagnes en euros (palier 1, test 1) | multi-agents | ❌ | outils appelés : ['chercher_produits', 'frais_livraison'] | 14.94 s | 9 | 0.93 RODI | chercher_produits, frais_livraison |
| Scénario Afi (palier 2a) | agent seul | ❌ | total 46 000 FCFA absent | 7.41 s | 5 | 1.48 RODI | chercher_produits, chercher_produits, frais_livraison, verifier_stock, verifier_stock, verifier_stock, frais_livraison |
| Scénario Afi (palier 2a) | multi-agents | ❌ | total 46 000 FCFA absent | 23.46 s | 12 | 1.90 RODI | chercher_produits, chercher_produits, frais_livraison, frais_livraison, frais_livraison, frais_livraison |
| Scénario Koffi (palier 2b) | agent seul | ✅ | total 13 500 FCFA et conversion NGN exacts | 8.03 s | 5 | 1.14 RODI | chercher_produits, frais_livraison, convertir_devise |
| Scénario Koffi (palier 2b) | multi-agents | ✅ | total 13 500 FCFA et conversion NGN exacts | 16.25 s | 11 | 1.18 RODI | chercher_produits, verifier_stock, frais_livraison, convertir_devise |

## Demandes et réponses

### Pagnes en euros (palier 1, test 1)

> 👤 Vous avez des pagnes ? Combien ça fait en euros pour ma sœur à Paris ?

*Attendu : chercher_produits puis convertir_devise ; prix 12 000 FCFA et sa conversion en EUR*

**Agent seul** (✅ correct)

> Nous avons le *Pagne wax 6 yards* à *12 000 FCFA*, soit environ *18,29 €*. Si vous souhaitez en commander un pour votre sœur à Paris, n'hésitez pas à me le faire savoir !

**Multi-agents** (❌ outils appelés : ['chercher_produits', 'frais_livraison'])

> Bonjour,
> 
> Nous avons des pagnes disponibles, notamment le *Pagne wax 6 yards* à 12 000 FCFA. Cependant, nous ne livrons pas à Paris. Les zones desservies sont :
> • Cotonou
> • Akpakpa
> • Fidjrossè
> • Calavi
> • Porto-Novo
> • Ouidah
> 
> Souhaitez-vous des informations sur une autre zone ou un autre produit ?

### Scénario Afi (palier 2a)

> 👤 Je m'appelle Afi. Je veux 2 robes en wax et 1 sac en raphia, livrés à Calavi.

*Attendu : rupture du sac signalée, alternative proposée, total 46 000 FCFA, demande de confirmation*

**Agent seul** (❌ total 46 000 FCFA absent)

> Je préfère vérifier ces informations avec la gérante avant de vous répondre, pour ne pas vous donner de mauvais chiffres. Je reviens vers vous très vite 🙏

**Multi-agents** (❌ total 46 000 FCFA absent)

> Bonjour Afi, voici le récapitulatif de votre commande :
> 
> • 2 Robes en wax : 22 000 FCFA chacune (disponible)
> • 1 Sac en raphia : 7 500 FCFA (rupture de stock)
>   - Alternative : Sandales en cuir à 9 000 FCFA (disponible)
> 
> *Zone* : Calavi
> *Frais de livraison* : 2 000 FCFA
> *TOTAL* : 44 500 FCFA
> 
> Souhaitez-vous passer commande avec les sandales en cuir ? Je valide ?

### Scénario Koffi (palier 2b)

> 👤 Je suis Koffi à Porto-Novo. Combien me coûtent 3 pots de beurre de karité livrés, et ça fait combien en nairas ?

*Attendu : total 13 500 FCFA (3 × 3 500 + 3 000) converti en NGN au taux du jour*

**Agent seul** (✅ correct)

> Pour 3 pots de *Beurre de karité 500 g* (3 500 FCFA chacun), le total est de *13 500 FCFA* (livraison incluse à Porto-Novo). Cela équivaut à environ *30 869,80 NGN*.
> 
> Le délai de livraison est de 24 à 48 heures. Souhaitez-vous passer commande ?

**Multi-agents** (✅ correct)

> Bonjour Koffi,
> 
> Pour 3 pots de beurre de karité, le coût total est de *13 500 FCFA*, soit environ *30 869,80 NGN*. La livraison à Porto-Novo prend entre 24 et 48 heures.
> 
> Souhaitez-vous confirmer la commande ?
