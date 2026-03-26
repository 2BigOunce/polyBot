# Contexte complet du projet — À lire avant toute intervention

## Résumé du projet

Bot Python de détection d'insider trading sur Polymarket + copy-trading automatique. Le bot tourne déjà et est en profit. Le repo est **privé**. L'étudiant (génie informatique, maîtrise le C, apprend Java) veut comprendre ce qu'il construit, pas vibecoder.

Stack : Python, web3.py, networkx, Alchemy WebSocket (Polygon), py-clob-client, SQLite, Telegram.

---

## État actuel du bot

Le bot est fonctionnel avec une stratégie simple : il détecte les dépôts de wallets en base de données et achète directement après la détection. Cette stratégie est profitable mais fragile — elle repose sur un signal unique (le dépôt) sans vérification supplémentaire.

### Problèmes identifiés avec la stratégie actuelle

1. **Signal trop faible** : un seul dépôt d'un wallet en DB déclenche un achat. Pas de scoring multi-facteurs, pas de confirmation comportementale.
2. **Vulnérabilité au baiting** : n'importe qui peut créer un wallet neuf, déposer une grosse somme, et le bot suit aveuglément. Un attaquant pourrait prendre la position inverse depuis un autre wallet et profiter du mouvement de prix causé par le bot.
3. **Wallet de trading identifiable on-chain** : même avec un repo privé, le pattern d'achat du bot est observable (achète systématiquement après qu'un wallet neuf dépose). Un observateur peut déduire la stratégie.
4. **Pas de sizing intelligent** : la taille de position ne tient pas compte du niveau de confiance du signal.

### Ce qui fonctionne bien

- La détection de dépôts on-chain est opérationnelle
- L'intégration CLOB pour placer des ordres fonctionne
- Le bot est en profit — la base technique est solide

---

## Améliorations prioritaires à intégrer

### 1. Scoring bayésien séquentiel (priorité haute)

Remplacer la logique binaire (wallet en DB → achète) par un scoring de suspicion cumulatif. Chaque signal observé met à jour un posterior bayésien :

```
log P(insider | données) = log P(insider) + Σ log P(D_k | insider) - log Z
```

Signaux à intégrer dans le scoring :
- Ancienneté du wallet (< 1 jour = très suspect, < 7 jours = suspect, > 30 jours = probablement pas un insider)
- Montant du dépôt par rapport au volume quotidien du marché ciblé
- Nombre de marchés ciblés (un insider mise sur 1-2 marchés max, pas 15)
- Proximité de la date de résolution du marché
- Nonce du wallet (nonce très bas = wallet neuf avec peu de transactions)
- Source de funding du wallet (traceable vers un CEX = plus dur à faker)
- Liquidité du marché ciblé (les marchés niche à faible volume sont plus susceptibles d'attirer des insiders)

Travailler en log-space pour la stabilité numérique (éviter l'underflow float).

**Ne trader que si le posterior dépasse un seuil de confiance.** Ce seuil est à calibrer sur les données existantes en DB — regarde les wallets passés qui étaient de vrais insiders (leur marché s'est résolu dans la direction de leur pari) et ceux qui étaient des faux positifs.

### 2. Pondération temporelle des signaux (priorité moyenne)

Un signal récent vaut plus qu'un vieux signal :

```
w_k = exp(-λ · (t - t_k))
```

Le signal on-chain (whale/dépôt) a un half-life d'environ 2 heures. Un dépôt détecté il y a 3 heures est moins actionnable qu'un dépôt de 5 minutes.

Intégrer ce poids dans le score bayésien pour prioriser les signaux frais dans les décisions de trading.

### 3. Entry condition améliorée (priorité haute pour le copy-trading)

Remplacer le simple `edge > 0.04` par :

```
|Edge| > τ_min + c_i
```

Où `c_i` est le coût effectif de transaction sur ce marché spécifique (dépend de la liquidité). Sur un marché peu liquide, le slippage est plus élevé → tu as besoin d'un edge plus grand.

**Règle absolue : ne jamais trader plus de 2% du volume 24h d'un marché.**

### 4. Position sizing — Half-Kelly (priorité haute)

```
f_prod = (p̂ - p) / (2 · (1 - p))
```

Toujours Half-Kelly en production. Ne jamais utiliser Full Kelly. Le score bayésien détermine p̂ (ta probabilité estimée que ce soit un vrai insider), et p est le prix actuel du marché.

Table de référence (bankroll = 100K) :

| Edge (p̂ − p) | Market p | Half-Kelly | Position ($) |
|---------------|----------|------------|--------------|
| 0.10 | 0.50 | 10.0% | $10,000 |
| 0.15 | 0.45 | 13.6% | $13,636 |
| 0.20 | 0.40 | 16.7% | $16,667 |
| 0.30 | 0.35 | 23.1% | $23,077 |

### 5. Cluster analysis (priorité moyenne)

Observation terrain : les vrais clusters sont rares sur Polymarket. La majorité des insiders sont des wallets uniques (employés tech, fonctionnaires) qui ne fragmentent pas. Le clustering reste utile pour :
- Détecter les rares insiders sophistiqués qui fragmentent
- Détecter les **faux signaux coordonnés** (tentatives de baiting)
- Le fait qu'un wallet suspect n'ait PAS de cluster derrière lui n'est pas un red flag — c'est le cas normal

Implémenter avec networkx : BFS/DFS pour remonter le funding, centralité pour identifier les wallets pivots, composantes connexes pour les clusters évidents. Mais ne pas en faire le signal principal — le scoring bayésien individuel est plus important.

### 6. Protections anti-exploitation (priorité moyenne)

- Varier les montants de trading (ne pas toujours miser la même somme)
- Ajouter un délai aléatoire entre détection et exécution (quelques secondes de jitter)
- Utiliser plusieurs wallets de trading si possible
- Monitorer si quelqu'un semble créer des faux signaux de manière répétée (wallets qui déposent gros puis ne tradent jamais, ou dont le trade perd systématiquement)

### 7. Cross-market correlation (priorité basse — phase bonus)

Quand un marché bouge suite à une news, les marchés corrélés mettent 12-45 secondes à s'ajuster. Détecter un mouvement sur un marché A et trader immédiatement les marchés corrélés B qui n'ont pas encore bougé.

### 8. News front-running (priorité basse — phase bonus)

Module complémentaire : détecter les résultats d'événements via des sources externes (APIs sport, RSS news, comptes X officiels) avant que le prix Polymarket ne converge. Fenêtre d'exploitation très courte, compétition forte. Se concentrer sur les marchés niche.

---

## Intelligence compétitive — Repos et outils à connaître

### Bots de trading open source exploitables

Ces repos exposent entièrement leur logique de trading. Les étudier permet de :
- Comprendre les failles des bots naïfs que d'autres font tourner
- Anticiper leurs comportements pour éviter de se retrouver en face
- S'inspirer des bonnes idées d'architecture

**Bots naïfs (logique simple, facilement prévisibles)** :
- `discountry/polymarket-trading-bot` — Flash crash strategy sur marchés 15min BTC/ETH/SOL. Seuils fixes, achète quand la probabilité chute soudainement. Pas de protection anti-manipulation.
- `dev-protocol/polymarket-trading-bot` — TypeScript, deux stratégies avec bandes de prix et conditions temporelles entièrement documentées. On sait exactement quand il achète et vend.
- `Gabagool2-2/polymarket-trading-bot-python` — Copy-trading des top traders Polymarket. Architecture Monitor → MongoDB → Executor. Si on identifie quels wallets il suit, on anticipe ses achats.
- `solship/Polymarket-Weather-Trading-Bot` — Bot météo, seuil d'entrée fixe (ENTRY_THRESHOLD=0.15), marchés de température. Complètement prévisible.

**Bots sophistiqués (à étudier pour se protéger)** :
- `dylanpersonguy/Fully-Autonomous-Polymarket-AI-Trading-Bot` — Ensemble multi-modèle (GPT-4o, Claude, Gemini), whale tracking, 15+ risk checks, Kelly sizing, Smart Money Index. Le plus complet en open source. Son whale tracker boost l'edge de +8% quand les whales convergent et pénalise de -2% quand elles divergent. C'est le type de bot qui pourrait nous détecter.
- `ent0n29/polybot` — Infrastructure Java/Spring Boot complète avec Kafka, ClickHouse, Grafana. Stratégie d'arbitrage sur marchés Up/Down. Over-engineered mais architecture intéressante.

**Détecteurs d'insiders (concurrents directs)** :
- `pselamy/polymarket-insider-tracker` — Le plus proche de notre projet. PostgreSQL + Redis, détection temps réel, wallet profiling (ancienneté, funding trail, impact sur l'order book). Score de confiance à 4 signaux. Bien structuré, bon README.
- `NickNaskida/polymarket-insider-bot` — Async Python, 100% écrit par IA. Plus simple que pselamy mais fonctionnel. SQLite, Slack alerting.
- `suislanchez/polymarket-insider-detector` — Approche statistique : p-value binomiale, analyse pre-resolution, whale detection, wallet clustering. Score composite 0-10. Intéressant pour la méthodologie statistique.

**Market makers (comprendre la contrepartie)** :
- `warproxxx/poly-maker` — Market making automatisé avec hyperparamètres via Google Sheets. Module poly_merger pour la consolidation de positions.
- `terrytrl100/polymarket-automated-mm` — Market making reward-optimized. Sélection automatique des marchés par profitabilité. Important à comprendre pour le liquidity trapping — ne pas se mettre en face d'un market maker par erreur.

**Outils d'analyse** :
- `NYTEMODEONLY/polyterm` — Terminal analytique : 20+ features dont whale tracking, insider scoring, arbitrage scanning, wash trade detection. SQLite local. 73 écrans TUI.
- `harish-garg/Awesome-Polymarket-Tools` — Liste curatée de tous les outils Polymarket.
- `aarora4/Awesome-Prediction-Market-Tools` — Même chose, plus large (inclut Kalshi, PredictIt).

### Plugin OpenClaw — Polyclaw

`chainstacklabs/polyclaw` — Skill Polymarket pour OpenClaw (système de skills de Claude Code). Browse marchés, placer des trades, tracker les positions, découvrir des hedges via analyse LLM.

Structure pertinente :
- `scripts/markets.py` — Browse via Gamma API
- `scripts/trade.py` — Exécution split + CLOB
- `scripts/positions.py` — Tracking P&L
- `scripts/hedge.py` — Découverte de hedges par logique contrapositionnelle LLM
- `lib/clob_client.py` — Wrapper py-clob-client avec gestion proxy Cloudflare

L'approche de hedge discovery est originale : le LLM analyse les paires de marchés pour des implications logiques (si "X gagne" implique "Y perd", alors YES sur X + NO sur Y = couverture). Applicable pour diversifier le risque quand on copie un insider.

### SDK officiel Polymarket

- `Polymarket/py-clob-client` — Client Python officiel. REST + WebSocket. Ordres limit, market, FOK. Gestion API keys via signature EIP-712.
- `Polymarket/agents` — Framework officiel pour bots IA sur Polymarket. Connecteurs modulaires (Gamma API, Chroma DB).

### Outils de tracking en production (pas open source)

- **Polysights / Insider Finder** — 24k users, revendique 85% win rate sur trades flaggés. Catégories : Sports, Politics, Crypto.
- **Polywhaler** — Whale tracker, trades > $10k, insider detection.
- **PolyInsider** — Dashboard temps réel, fresh wallet monitoring ($5k+ first bets).
- **Polymarket Analytics** — Analytics cross-platform (Polymarket + Kalshi).

---

## Marchés les plus ciblés par les insiders

Par ordre de fréquence documentée :

1. **Annonces tech & produits** — OpenAI, Google, Apple. Cas : OpenAI browser ($40k), Google Year in Search ($3M → $4.15M).
2. **Géopolitique & gouvernement** — Venezuela/Maduro ($30k → $400k+), Prix Nobel, pardons présidentiels.
3. **Marchés crypto** — Lancements de tokens, exposés (ZachXBT).
4. **FDA & régulation** — Approbations de médicaments.
5. **Célébrités & entertainment** — Taylor Swift/Kelce (achat 15 min avant annonce).

Profil type : **faible volume** (niche), **résolution binaire avec date précise**, **cercle restreint de personnes informées**.

---

## Menaces et protections

### Comment le bot peut se faire exploiter

1. **Faux insiders** : wallet neuf + gros dépôt → notre bot copie → l'attaquant est en face. Le scoring bayésien multi-facteurs réduit ce risque.
2. **Pattern observable on-chain** : notre wallet achète après des dépôts de wallets neufs. Solution : jitter aléatoire + variation montants + plusieurs wallets.
3. **Publication future GitHub** : ne JAMAIS publier les seuils exacts, la logique de scoring finale, ni les paramètres de trading.

### Stratégies connues pour exploiter les bots naïfs (connaissance défensive)

1. **Baiting / Spoofing** — Gros ordre visible pour déclencher les bots, puis annulation. Risque sur CLOB : exécution avant annulation.
2. **Sandwich de latence** — Acheter avant un bot prévisible, laisser son achat pousser le prix, revendre.
3. **Exploitation des repos publics** — Lire le code, comprendre les seuils, créer les conditions de déclenchement. C'est pourquoi notre repo est privé.
4. **Faux signaux sur sources de données** — Faux sentiment Twitter/Reddit pour tromper les bots NLP. Rappel : signaux social media = bruit en dessous de 2h avant résolution.
5. **Liquidity trapping** — Market making des deux côtés du spread, laisser les bots naïfs croiser.

---

## Comment appliquer ces améliorations

**Regarde d'abord les données en DB** avant de tout implémenter. Analyse les wallets passés :
- Combien étaient de vrais insiders (marché résolu dans leur direction) ?
- Combien étaient des faux positifs ?
- Quels signaux auraient permis de distinguer les deux catégories ?
- Y a-t-il des patterns de faux signaux récurrents ?

Si win rate actuel > 80% → scoring bayésien = amélioration incrémentale.
Si win rate < 70% → scoring bayésien = urgence.

**Ordre de priorité** :
1. Half-Kelly + limite 2% volume → protège le capital immédiatement
2. Scoring bayésien → améliore la qualité des signaux
3. Entry condition améliorée → évite les trades à edge négatif après slippage
4. Délai aléatoire + variation des montants → réduit la prévisibilité
5. Cluster analysis → signal bonus
6. Cross-market + news → phases futures

**Approche** : une amélioration à la fois, testée sur les données historiques en DB, puis déployée. Ne pas tout changer en même temps.

---

## Fichiers de référence

- `references/synthese_insider_bot.md` — Formules complètes (Kelly, Bayes séquentiel log-space, VaR, Signal Decay, Entry Conditions, Cross-Market Correlation, tableau d'allocation Kelly)
- `ROADMAP_polymarket_insider_detector.md` — Roadmap avec 12 checkpoints de réflexion pédagogiques

---

## Rappel pédagogique

L'étudiant veut apprendre. Avant d'implémenter une amélioration :
1. Explique le problème que ça résout
2. Demande-lui comment il l'aborderait
3. Discute son raisonnement
4. Implémente ensemble

Ne pas tout coder d'un coup. Une amélioration à la fois, testée, comprise, puis la suivante.
Faire les parallèles avec le C et Java quand un concept Python est nouveau.
Expliquer ce qui se passe sous le capot (web3.py, asyncio, networkx) — pas de magie noire.
