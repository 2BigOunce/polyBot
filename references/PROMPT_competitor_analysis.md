# Analyse du bot concurrent — Latence et profitabilité

## Objectif

Analyser le wallet `0x99558e8d51b707069ddd64e8583c699bda94e3e6` qui fait de l'arbitrage post-résolution sur Polymarket. On veut :
1. Lister ses trades > 200$ et calculer ses profits
2. Estimer sa latence en mesurant le délai entre la résolution d'un marché et son trade

## Méthode d'estimation de latence

Pour chaque trade du concurrent sur un marché résolu :

1. Extraire le `conditionId` depuis les events de la transaction (visible dans les events `PositionSplit` ou `OrderFilled` sur les contrats Polymarket)
2. Trouver la transaction de résolution correspondante — chercher l'event `ConditionResolution` sur le contrat Conditional Tokens (`0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`) avec le même `conditionId`
3. Comparer les blocs et timestamps :
   - Bloc et timestamp de la résolution
   - Bloc et timestamp du trade du concurrent
   - Delta = timestamp_trade - timestamp_résolution

Ce delta est une **borne supérieure** de sa latence réelle (car l'opérateur Polymarket ajoute du délai pour soumettre le match on-chain). Mais si le delta est consistant sur plusieurs trades, ça donne une bonne approximation.

Pour les marchés courts (sports, crypto 15min), la résolution peut utiliser Chainlink Data Streams au lieu de UMA — vérifier aussi les events de résolution Chainlink.

## Transactions de référence

```
0x9b195a019a80167fba688237e52316013cf5b9ee53aaf7cf6341c9a94a931825
0xa52c65f882075a7b885aa89b68b41c090d1ccc02a4bf200e19055578731b2bc1
0xc7a69550612ec69f5a0e16104d24853d5b0f72d93426281f4b78acccfbc4f8d3
0xfbe19dc94726bf1b3644fae64bbc52129eefcfda2e0068392ad5b5639564172f
0x7568079096d9c81d0ea25727c3ab4febc824e845a2d4c55c27737c63e4c6c767
0x3b77a7343c29232713684ebe93ecaccf8ccb6fbdbb08f48c2cc9578a8d861aa5
```

## Étapes concrètes

### Étape 1 — Décoder chaque transaction

Pour chacune des 6 transactions ci-dessus :
- Récupérer le receipt complet (events/logs)
- Dans les events `OrderFilled` (topic0 = `0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6`) extraire :
  - `maker` et `taker` — identifier qui est le concurrent et qui est la contrepartie
  - `makerAmountFilled` / `takerAmountFilled` — montants en USDC (6 decimals) et shares
  - `fee`
- Dans les events `PositionSplit` ou `TransferBatch` extraire le `conditionId`
- Identifier le token ID pour mapper vers le marché Polymarket (via Gamma API : `https://gamma-api.polymarket.com/markets?clob_token_ids=TOKEN_ID`)

### Étape 2 — Trouver la résolution de chaque marché

Pour chaque `conditionId` trouvé :
- Chercher l'event `ConditionResolution` sur le contrat `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`
- Filtrer les events avec le même `conditionId` dans les topics
- Extraire le bloc et le timestamp de cet event
- Si pas trouvé, chercher via l'UMA Adapter (`0x6A9D222...`) ou Chainlink

### Étape 3 — Calculer le delta pour chaque trade

```
delta_blocs = bloc_trade - bloc_résolution
delta_secondes = timestamp_trade - timestamp_résolution
```

### Étape 4 — Récupérer les autres trades > 200$

Utiliser l'API PolygonScan pour lister toutes les transactions du wallet :
```
https://api.polygonscan.com/api?module=account&action=txlist&address=0x99558e8d51b707069ddd64e8583c699bda94e3e6&sort=desc
```
(nécessite une API key PolygonScan, ou utiliser notre nœud Chainstack directement)

Filtrer les transactions qui interagissent avec les contrats Polymarket et dont le montant USDC > 200$. Répéter les étapes 1-3 pour chacune.

### Étape 5 — Produire le rapport

Tableau final attendu :

```
| TX Hash (short) | Date | Marché | Montant USDC | Profit estimé | Blocs après résolution | Secondes après résolution |
|-----------------|------|--------|-------------|---------------|----------------------|--------------------------|
| 0x9b19...       | ...  | ...    | $8,415      | ...           | X blocs              | Y secondes               |
```

Plus un résumé :
- Latence moyenne (en blocs et secondes)
- Latence min/max
- Profit total estimé
- Fréquence des trades
- Types de marchés ciblés

## Infos techniques utiles

- Polygon block time : ~2 secondes
- USDC.e sur Polygon : 6 decimals (1000000 = 1 USDC)
- Contrats Polymarket clés :
  - Conditional Tokens : `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`
  - Neg Risk CTF Exchange : `0xC5d563A36AE78145C45a50134d48A1215220f80a`
  - CTF Exchange : `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`
  - Neg Risk Adapter : `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296`
  - Neg Risk Fee Module 2 : `0xB768891e3130F6dF18214Ac804d4DB76c2C37730`
  - Wrapped Collateral : `0x3A3BD7bb9528E159577F7C2e685CC81A765002E2`
- ConditionResolution event signature : `ConditionResolution(bytes32 indexed conditionId, address indexed oracle, bytes32 indexed questionId, uint256 outcomeSlotCount, uint256[] payoutNumerators)`
- OrderFilled event signature : `OrderFilled(bytes32 indexed orderHash, address indexed maker, address indexed taker, uint256 makerAssetId, uint256 takerAssetId, uint256 makerAmountFilled, uint256 takerAmountFilled, uint256 fee)`

## Ce qu'on veut savoir au final

1. **Ce bot est-il rapide ?** Si son delta moyen est < 10 secondes (< 5 blocs), c'est un concurrent sérieux avec probablement un setup optimisé (Rust + co-location). Si c'est > 30 secondes, on peut le battre avec notre setup actuel.
2. **Combien il gagne ?** Pour savoir si ça vaut le coup de l'affronter sur les mêmes marchés.
3. **Quels marchés il cible ?** Pour identifier les marchés qu'il ne couvre pas et qu'on pourrait exploiter sans compétition.
