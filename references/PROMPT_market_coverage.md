# Maximiser la couverture de marchés — Arb post-résolution

## Contexte

Le bot fait de l'arbitrage post-résolution sur Polymarket : il capte les ordres stale laissés par les market makers après la résolution d'un marché. Le bot est déjà fonctionnel et profitable. Le setup est optimisé (VPS Londres, nœud Chainstack privé Londres, WebSocket, book en mémoire, ordres pré-calculés).

L'edge principal n'est plus la vitesse mais la **couverture** : plus on surveille de marchés illiquides, plus on capte d'opportunités. Actuellement, le bot ne couvre qu'une partie des marchés. L'objectif est de maximiser le nombre de marchés surveillés automatiquement.

**Note** : certaines de ces fonctionnalités sont peut-être déjà implémentées. Commence par lire le code existant et identifie ce qui manque avant de coder quoi que ce soit.

## Pipeline cible

```
Scan Gamma API (périodique)
    → Filtre marchés illiquides proches de résolution
    → Scan order books pour ordres stale potentiels
    → Subscribe WebSocket sur les candidats
    → Écoute résolutions en batch (un seul filtre de logs on-chain)
    → Exécution immédiate sur match
```

## Étape 1 — Scanner tous les marchés actifs

Si ce n'est pas déjà fait, implémenter un scan périodique de l'API Gamma :

```
GET https://gamma-api.polymarket.com/markets?active=true&closed=false
```

Fréquence : toutes les 1-5 minutes suffit, ce n'est pas du hot path.

Pour chaque marché, récupérer :
- `condition_id` / `clob_token_ids` (pour le tracking)
- `volume` et `liquidity` (pour identifier les illiquides)
- `end_date_iso` ou `resolution_date` (proximité de résolution)
- `accepting_orders` (vérifier que le marché est tradeable)
- `neg_risk` et `minimum_tick_size` (pour la construction d'ordres)
- `fee_rate_bps` si applicable (pour inclure dans le signing)

## Étape 2 — Filtrer les marchés candidats

Critères de sélection (à ajuster selon les données) :
- **Volume quotidien faible** : les marchés à faible volume ont des MM moins réactifs → ordres stale plus probables
- **Proche de la résolution** : marchés dont la date de résolution est dans les prochaines heures/jours
- **Ordres dans le book** : pas d'intérêt à surveiller un marché avec un order book vide
- **Pas de marchés à résolution ultra-rapide (5min/15min crypto)** : trop compétitifs, dominés par des bots HFT

Le filtre doit être dynamique — recalculé à chaque scan.

## Étape 3 — Pré-identifier les ordres stale potentiels

Avant la résolution, scanner les order books des marchés candidats :

```
GET https://clob.polymarket.com/book?token_id=TOKEN_ID
```

Chercher les ordres limit à des prix décalés du prix actuel. Par exemple :
- Marché actuellement à 0.90 → ordres d'achat YES à 0.20-0.40 sont des candidats stale si le marché resolve YES
- Marché actuellement à 0.10 → ordres d'achat YES à 0.05-0.30 deviennent profitables si le marché resolve NO (les shares NO valent alors 1$)

Pour chaque marché candidat, pré-calculer :
- **Scénario YES** : quels ordres stale seraient profitables, volume total capturable, profit estimé
- **Scénario NO** : idem
- **Score du marché** : profit_potentiel × probabilité_d'ordres_stale

Stocker ces infos en mémoire, prêtes à être utilisées au moment de la résolution.

## Étape 4 — WebSocket multiplexé sur les candidats

Maintenir un WebSocket ouvert vers le CLOB pour les marchés candidats :

```
wss://ws-subscriptions-clob.polymarket.com/ws/
```

Subscribe au market channel pour chaque marché candidat → garder le book à jour en mémoire en temps réel. Quand un ordre est ajouté ou annulé dans le book, mettre à jour les pré-calculs du profit potentiel.

Limites à vérifier :
- Combien de marchés peut-on subscribe simultanément sur un seul WebSocket ?
- Si limite atteinte, ouvrir plusieurs connexions ou prioriser par score

## Étape 5 — Écouter les résolutions en batch

C'est le point clé pour la scalabilité. Au lieu d'écouter les events de résolution marché par marché, écouter TOUS les events `ConditionResolution` sur le contrat Conditional Tokens en un seul filtre :

```python
# Pseudocode — écouter toutes les résolutions
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# Event signature: ConditionResolution(bytes32 indexed conditionId, address indexed oracle, bytes32 indexed questionId, uint256 outcomeSlotCount, uint256[] payoutNumerators)
CONDITION_RESOLUTION_TOPIC = web3.keccak(text="ConditionResolution(bytes32,address,bytes32,uint256,uint256[])")

# Un seul filtre pour TOUTES les résolutions
filter = {
    "address": CTF_CONTRACT,
    "topics": [CONDITION_RESOLUTION_TOPIC]
}

# Quand un event arrive, extraire le conditionId et vérifier s'il est dans notre liste de candidats
```

Dès qu'une résolution arrive :
1. Extraire le `conditionId` et les `payoutNumerators` (pour savoir quel outcome a gagné)
2. Vérifier si ce marché est dans la liste des candidats
3. Si oui, récupérer les pré-calculs (ordres stale identifiés, profit estimé)
4. Envoyer immédiatement l'ordre pré-calculé correspondant au scénario de résolution

## Étape 6 — Priorisation dynamique

Si le nombre de marchés candidats dépasse les capacités (WebSocket, mémoire, etc.), scorer et prioriser :

```
score = profit_potentiel_estimé × (1 / temps_avant_résolution) × facteur_illiquidité
```

- `profit_potentiel_estimé` : somme des profits sur ordres stale identifiés dans le book
- `temps_avant_résolution` : les marchés proches de la résolution sont prioritaires
- `facteur_illiquidité` : les marchés très illiquides ont plus de chances d'avoir des ordres oubliés

Mettre à jour le scoring à chaque scan Gamma et à chaque mise à jour du book via WebSocket.

## Considérations techniques

### Rate limits
- Gamma API : rate limits généreux pour la lecture
- CLOB book queries : attention au polling trop fréquent, préférer le WebSocket
- Le rate limiting CLOB utilise Cloudflare (throttling, pas rejet) avec burst autorisé sur le trading

### Taker fees post-résolution
Les taker fees sont quasi nulles pour l'arb post-résolution car la formule `fee = C × 0.25 × (p × (1-p))²` tend vers zéro aux extrêmes de prix. Un ordre stale à 0.20$ sur un marché résolu génère un fee de ~0.64% pour un profit brut de ~80%. Négligeable.

### Le délai 500ms a été supprimé (18 février 2026)
Les ordres taker s'exécutent maintenant instantanément. Les market makers n'ont plus de fenêtre de grâce pour annuler. C'est un avantage direct pour l'arb post-résolution — plus d'ordres stale restent dans le book.

### Types de résolution
- **Marchés longs (politique, tech, etc.)** : résolution via UMA Optimistic Oracle → event `ConditionResolution` on-chain
- **Marchés courts (sports, crypto 5min/15min)** : résolution via Chainlink Data Streams → résolution automatique, plus rapide

Vérifier que le listener gère les deux types.

### feeRateBps dans le signing
Depuis janvier 2026, les ordres doivent inclure le champ `feeRateBps` dans le payload signé pour les marchés avec fees. Vérifier que le bot query le fee rate avant de construire l'ordre :
```
GET https://clob.polymarket.com/fee-rate?tokenID=TOKEN_ID
```

## Métriques à tracker

Une fois implémenté, logger :
- Nombre de marchés surveillés à chaque instant
- Nombre de résolutions détectées par jour
- Nombre d'ordres stale identifiés vs captés
- Taux de réussite (ordres envoyés vs ordres matchés)
- Profit par trade et profit total
- Latence : timestamp résolution → timestamp ordre envoyé

Ces métriques permettront d'affiner les critères de sélection et de priorisation.
