# Synthèse Technique — Insider Tracking Bot Polymarket

> Éléments actionnables extraits du guide Claude Skills + pages Prediction Market Trading Bot

---

## Partie 1 — Architecture du bot de trading

Les deux pages décrivent une architecture en 6 phases pour un Prediction Market Trading Bot. L'architecture est techniquement solide et applicable au projet de détection d'insiders sur Polymarket.

### 1.1 Pipeline en 6 phases

| Phase | Nom      | Type           | Description |
|-------|----------|----------------|-------------|
| 1     | Scan     | SEQUENTIAL     | Scanne 300+ marchés actifs filtrés par liquidité, volume, time-to-resolution. Détecte les mouvements de prix anormaux et les spreads larges. |
| 2     | Research | MCP ENHANCED   | Agents parallèles scrappent Twitter, Reddit, RSS. Classification NLP du sentiment par marché — compare le signal narratif aux cotes actuelles. |
| 3     | Predict  | DOMAIN INTEL   | XGBoost + LLM calibrent la probabilité réelle vs prix de marché. Le consensus ne se déclenche que si la confiance dépasse un seuil. |
| 4     | Risk     | CONTEXT-AWARE  | Plusieurs agents valident indépendamment. Kelly size la position. Bloque sur tout dépassement de max-bet / VAR / exposition. |
| 5     | Execute  | SEQUENTIAL     | Ordres placés via CLOB API on-chain. Slippage monitoré. Auto-hedge si les conditions changent avant settlement. |
| 6     | Compound | ITERATIVE      | 5 agents extraient cause + prévention des pertes, sauvent dans une base de connaissance. Les futurs scans consultent les cas passés en premier. |

### 1.2 Règles de risque critiques

Avant tout appel à execute, vérifier que TOUTES les règles suivantes passent :

1. `edge = p_model - p_market > 0.04`
2. `size <= kelly(f, bankroll)`
3. `exposure + bet <= max_exposure`
4. `VaR(95%) within daily limit`

Ces 4 règles doivent être implémentées dans un script déterministe (`validate_risk.py`), pas en langage naturel — le code est déterministe, les instructions en langage naturel ne le sont pas.

### 1.3 Formules mathématiques de référence

| Catégorie | Formule | Usage |
|-----------|---------|-------|
| Expected Value | `EV = p · b − (1 − p)` | p = prob modèle, b = cotes décimales − 1 |
| Market Edge | `edge = p_model − p_mkt` | Trader seulement si edge > 0.04 |
| Kelly Criterion | `f* = (p · b − q) / b` | q = 1 − p. Fraction max du bankroll |
| Fractional Kelly | `f = α · f*, α ∈ (0, 1]` | Utiliser α = 0.25–0.5 pour réduire la variance |
| Mispricing Score | `δ = (p_model − p_mkt) / σ` | Z-score de la divergence modèle vs marché |
| VaR 95% | `VaR = μ − 1.645 · σ` | Perte max journalière à 95% de confiance |
| Max Drawdown | `MDD = (Peak − Trough) / Peak` | Bloquer nouveaux trades si MDD > 8% |
| Sharpe Ratio | `SR = (E[R] − Rf) / σ(R)` | Objectif SR > 2.0 |
| Brier Score | `BS = (1/n) · Σ(pᵢ − oᵢ)²` | Mesure la calibration du modèle, plus bas = meilleur |
| Bayes Update | `P(H|E) = P(E|H)·P(H) / P(E)` | Mettre à jour le prior à chaque signal — voir §1.4 pour version séquentielle log-space |
| Signal Decay | `w_k = exp(-λ · (t - t_k))` | Pondération temporelle des signaux — voir §1.5 |
| Entry (améliorée) | `|Edge| > τ_min + c_i` | Intègre le coût de transaction — voir §1.6 |
| Cross-Market Edge | `Edge_B = P(B|A_new) - p_B` | Arbitrage inter-marchés corrélés — voir §1.7 |
| Volume Limit | `δ ≤ 0.02 · V_24h` | Ne jamais trader plus de 2% du volume 24h |
| Profit Factor | `PF = gross_profit / gross_loss` | Bot sain maintient PF > 1.5 |
| Arb Condition | `Σ(1/oddsᵢ) < 1 → profit` | Somme des cotes réciproques < 1 |

### 1.4 Bayes séquentiel — Scoring de suspicion (Phases 2-3)

Le théorème de Bayes simple met à jour une croyance avec un seul data point. En pratique, pour chaque wallet suspect, on observe une **séquence** d'actions (dépôt, attente, pari, etc.). Chaque action D_k met à jour la probabilité que le wallet soit un insider :

```
P(H | D_1, ..., D_t) ∝ P(H) · Π P(D_k | H)    pour k = 1..t
```

**Implémentation en log-space** (numériquement stable — évite l'underflow float) :

```
log P(H | D) = log P(H) + Σ log P(D_k | H) - log Z
```

où Z est la constante de normalisation.

Concrètement pour le bot :
- Prior P(H) = probabilité de base qu'un wallet soit insider (ex: 0.01)
- Chaque signal augmente ou diminue le posterior :
  - Wallet < 1 jour → fort likelihood ratio
  - Dépôt > 10k USDC → fort likelihood ratio
  - Pari sur un seul marché niche → fort likelihood ratio
  - Activité variée sur plusieurs marchés → diminue le posterior

Le scoring bayésien est plus rigoureux que d'empiler des signaux binaires (flag oui/non) car il quantifie la confiance cumulée.

### 1.5 Signal Decay — Pondération temporelle (Phase 3)

Un signal d'insider vieillit. Un gros dépôt détecté il y a 5 minutes est plus "chaud" qu'un dépôt d'il y a 3 heures. La pondération exponentielle :

```
w_k = exp(-λ · (t - t_k))
```

où λ contrôle la vitesse de décroissance. Taux de décroissance par source :

| Source | λ | Half-life | Poids à t + 1h |
|--------|---|-----------|-----------------|
| API / odds feed | 2.3 × 10⁻³ | 5 min | 0.00 |
| News NLP | 1.9 × 10⁻⁴ | 1 heure | 0.50 |
| Social (X/Reddit) | 3.9 × 10⁻⁴ | 30 min | 0.25 |
| On-chain / whale | 9.6 × 10⁻⁵ | 2 heures | 0.71 |

**Application au scoring de clusters** : quand tu calcules le score de suspicion d'un cluster, pondère chaque signal par son âge. Un cluster dont l'activité date de 4 heures est moins prioritaire pour le copy-trading qu'un cluster actif dans les dernières minutes.

**Note pratique** : les signaux social media (X, Reddit) sont du bruit en dessous de 2h avant résolution d'un marché. Ne pas les utiliser pour les marchés à résolution imminente.

### 1.6 Entry Condition améliorée (Phase 4)

La règle simple `edge > 0.04` ne tient pas compte du coût de transaction. Version améliorée :

```
|Edge_i| > τ_min + c_i
```

où :
- `τ_min = 0.05` (5 cents d'edge minimum)
- `c_i = (p_i · (1 - p_i) / b) · δ_min` — le coût de déplacer le prix en fonction de la liquidité du marché

Sur un marché peu liquide, c_i est plus élevé → tu as besoin d'un edge plus grand pour compenser le slippage.

**Règle pratique de taille** : ne jamais trader plus de 2% du volume 24h d'un marché. Au-delà, l'impact sur le prix détruit l'edge.

### 1.7 Cross-Market Correlation (Phase bonus)

Quand un marché A bouge suite à une news, les marchés corrélés B mettent du temps à s'ajuster :

```
Edge_B = P(B | A_new) - p_B_market
```

Le délai de propagation empirique est de 12-45 secondes selon la profondeur du marché. C'est une source d'alpha exploitable pour la phase bonus (news arbitrage) : quand tu détectes un mouvement sur un marché, vérifie immédiatement les marchés corrélés qui n'ont pas encore bougé.

### 1.8 Kelly pour marchés binaires — Référence rapide (Phase 4)

Formule simplifiée pour prediction markets (payout binaire) :

```
f* = (p̂ - p) / (1 - p)          # Full Kelly
f_prod = f* / 2 = (p̂ - p) / (2(1 - p))   # Half-Kelly (production)
```

**Règle de production : toujours utiliser Half-Kelly.** Réduit la variance de 75%.

Tableau d'allocation (bankroll = 100K) :

| Edge (p̂ − p) | Market p | Full Kelly | Half-Kelly | Position ($) |
|---------------|----------|------------|------------|--------------|
| 0.10 | 0.50 | 20.0% | 10.0% | $10,000 |
| 0.15 | 0.45 | 27.3% | 13.6% | $13,636 |
| 0.20 | 0.40 | 33.3% | 16.7% | $16,667 |
| 0.30 | 0.35 | 46.2% | 23.1% | $23,077 |
| 0.46 | 0.45 | 83.6% | 41.8% | $41,818 |

**Ne JAMAIS utiliser Full Kelly sur des marchés à résolution < 5 min.** La variance est trop élevée et l'estimation de p̂ trop incertaine.

### 1.9 Structure de fichiers recommandée

```
predict-market-bot/
├── SKILL.md              # triggers + règles core
├── scripts/
│   ├── validate_risk.py  # vérifications déterministes
│   └── kelly_size.py     # calculateur de position
└── references/
    └── formulas.md       # référence mathématique complète
```

---

## Partie 2 — Guide Claude Skills

### 2.1 Structure minimale d'une Skill

```
ton-skill/
├── SKILL.md       # OBLIGATOIRE — instructions en Markdown avec YAML frontmatter
├── scripts/       # optionnel — code Python, Bash
├── references/    # optionnel — documentation chargée à la demande
└── assets/        # optionnel — templates, configs
```

### 2.2 Frontmatter YAML

```yaml
---
name: insider-tracker
description: >
  Détecte les wallets insiders sur Polymarket. Utiliser pour :
  analyse de clusters, funding chain, copytrade.
  Triggers : 'analyser wallet', 'détecter insider', 'cluster'.
metadata:
  author: Ben
  version: 1.0.0
---
```

Règles absolues du nom : kebab-case uniquement, pas d'espaces, pas de majuscules, pas de 'claude' ou 'anthropic' dans le nom, pas de balises XML (< >) dans le frontmatter.

### 2.3 Principe de Progressive Disclosure

La Skill utilise 3 niveaux de chargement pour minimiser les tokens :

- **Niveau 1** — YAML frontmatter : chargé en permanence dans le system prompt. Indique à Claude quand utiliser la Skill.
- **Niveau 2** — Corps du SKILL.md : chargé uniquement quand la Skill est pertinente. Contient les instructions complètes.
- **Niveau 3** — Fichiers liés (references/) : chargés uniquement si nécessaire. Formules, docs API, exemples détaillés.

Garder le SKILL.md sous 5000 mots, et déplacer les formules mathématiques, la doc de l'API Polymarket, et les patterns de graphes dans `references/`.

### 2.4 Les 5 patterns d'architecture applicables

**Pattern 1 — Sequential Workflow Orchestration** : Idéal pour le pipeline de détection : Monitoring → Analyse → Alerting → CopyTrade. Chaque étape a une validation avant de passer à la suivante.

**Pattern 2 — Multi-MCP Coordination** : Coordonne plusieurs services en parallèle : Alchemy RPC (blockchain) + Polymarket CLOB API (marchés) + base de données locale (historique des wallets).

**Pattern 3 — Iterative Refinement** : S'applique à l'analyse de clusters : générer un premier graphe → valider la qualité → affiner les seuils de détection → re-valider jusqu'au seuil de qualité.

**Pattern 4 — Context-Aware Tool Selection** : Sélection dynamique selon le contexte : nouveau wallet non connu → analyse complète BFS. Wallet déjà vu → consultation du cache uniquement.

**Pattern 5 — Domain-Specific Intelligence** : Le plus pertinent : embarque l'expertise domaine directement dans la logique. Règles de détection d'insiders (seuils de dépôt, timing des trades, taille des positions) encodées comme règles de compliance — vérification AVANT toute action.

### 2.5 Critères de succès à viser

| Métrique | Cible | Comment mesurer |
|----------|-------|-----------------|
| Skill trigger | 90% des requêtes pertinentes | Tester 10-20 requêtes, compter les chargements automatiques |
| Tool calls par workflow | Minimiser vs baseline | Comparer avec/sans Skill sur même tâche |
| Appels API échoués | 0 par workflow | Monitor les logs MCP pendant les tests |
| Résultats consistants | Même output 3-5 runs | Comparer structure + qualité des outputs répétés |

### 2.6 Debugging des Skills — checklist

- Skill ne se déclenche pas → description trop vague, ajouter des trigger phrases spécifiques
- Skill se déclenche trop souvent → ajouter des triggers négatifs ('Do NOT use for...')
- Instructions ignorées → les mettre en tête de fichier, utiliser ## CRITICAL, les rendre plus courtes
- Appels MCP échoués → tester le MCP indépendamment avant de tester la Skill
- Demander à Claude : 'Quand utiliserais-tu la skill insider-tracker ?' → il cite la description

---

## Partie 3 — Application concrète au projet

### 3.1 Mapping des phases du bot vers le projet existant

| Phase bot | Équivalent dans le projet | Priorité |
|-----------|--------------------------|----------|
| Scan — nouveaux marchés | Monitoring Polygon : nouveaux wallets + gros dépôts | Phase 1 ✅ |
| Research — sentiment NLP | Bonus phase news arbitrage (latence events vs prix) | Phase 5 — futur |
| Predict — XGBoost + LLM | Calcul de l'edge : p_insider vs p_market | Phase 3 — analyse clusters |
| Risk — Kelly + VAR | Sizing des positions + règles max-bet | Phase 4 — copytrade |
| Execute — CLOB API | py-clob-client pour placer les ordres | Phase 4 — copytrade |
| Compound — knowledge base | Historique des wallets détectés + patterns connus | Phase 2-3 |

### 3.2 Skill insider-tracker — structure suggérée

```
insider-tracker/
├── SKILL.md
│   ├── [YAML] name, description, triggers
│   ├── Phase 1 : règles de détection (seuils dépôt, timing)
│   ├── Phase 2 : algorithmes BFS/DFS funding chain
│   ├── Phase 3 : clustering (centralité, composantes connexes)
│   └── Phase 4 : règles copytrade + risk management
├── scripts/
│   ├── validate_risk.py      # Kelly + VaR + max-bet checks
│   ├── kelly_size.py          # calculateur de position
│   └── cluster_analysis.py    # graphe networkx
└── references/
    ├── formulas.md            # toutes les formules de référence
    ├── polymarket_api.md      # doc CLOB API
    └── graph_patterns.md      # patterns BFS/DFS/centralité
```

### 3.3 Règles de risque à implémenter en priorité

Ces 4 règles doivent être dans `validate_risk.py` et appelées AVANT chaque ordre :

- `edge = p_insider_estimate - p_market > 0.04` — ne copier que si l'edge est significatif
- `size <= kelly(f, bankroll)` — ne jamais dépasser la fraction Kelly du bankroll
- `exposure + bet <= max_exposure` — limite d'exposition totale sur Polymarket
- `VaR(95%) within daily limit` — stopper si la perte journalière dépasse le seuil

Le Fractional Kelly (α = 0.25 à 0.5) est recommandé car les probabilités d'un insider sont estimées, pas certaines. α = 0.25 est un bon point de départ.

### 3.4 Avertissements sur les métriques

Les métriques de backtest (Win Rate 68.4%, Sharpe 2.14, Max DD -4.2%) sont probablement sur-optimisées. Pour évaluer honnêtement le bot :

- **Brier Score** : mesure la calibration des estimations de probabilité
- **Profit Factor > 1.5** : ratio brut gain/perte sur trades réels
- **Sharpe > 1.0** : objectif réaliste pour débuter
- **Max DD < 15%** : seuil de sécurité raisonnable pour arrêter le bot automatiquement

---

## Partie 4 — Prompt de démarrage pour Claude Code

```
Tu travailles sur un bot Python de détection d'insider trading sur Polymarket.

Stack : Python, web3.py, networkx, Alchemy RPC, py-clob-client.

Objectifs du projet :
1. Monitorer la blockchain Polygon pour détecter les nouveaux wallets
   qui font de gros dépôts puis placent des paris importants (suspects).
2. Identifier des clusters de wallets probablement contrôlés par
   le même insider via analyse de graphe (BFS/DFS + centralité).
3. Copier automatiquement les trades des wallets détectés via
   py-clob-client avec risk management (Kelly Criterion + VaR).

Amélioration prioritaire : implémenter validate_risk.py avec ces règles :
- edge = p_insider_estimate - p_market > 0.04
- size <= kelly(f=0.25, bankroll)  # Fractional Kelly alpha=0.25
- exposure + new_bet <= max_exposure
- VaR(95%) = mu - 1.645*sigma <= daily_limit

Architecture cible (6 phases) :
Scan → Research → Predict → Risk → Execute → Compound

Contrainte : le projet sera open-source pour la partie détection,
le module de copytrade restera privé.

Commence par lire le code existant, puis propose les améliorations
en expliquant le raisonnement à chaque étape.
```

*Document généré le 8 mars 2026*
