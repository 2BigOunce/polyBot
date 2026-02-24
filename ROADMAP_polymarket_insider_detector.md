# Roadmap — Polymarket Insider Detector & Copy-Trader

## Instructions pour Claude Code

> **IMPORTANT** : Ce projet est aussi un projet d'apprentissage. L'utilisateur est étudiant en génie informatique et veut comprendre ce qu'il construit, pas juste vibecoder.
>
> **Règles à suivre :**
> 1. **Ne jamais écrire un module complet d'un coup.** Avant de coder une fonctionnalité non triviale, expliquer le problème, demander à l'utilisateur comment il l'aborderait, puis discuter de sa réponse avant d'implémenter.
> 2. **Points de réflexion 🧠** : Chaque fois qu'un point 🧠 apparaît dans cette roadmap, faire une PAUSE. Poser la question associée à l'utilisateur et attendre sa réponse. Discuter de son raisonnement, corriger si nécessaire, puis seulement ensuite coder ensemble.
> 3. **Expliquer le "pourquoi"** : Ne pas juste écrire du code — expliquer les choix d'architecture, les tradeoffs, pourquoi telle structure de données plutôt qu'une autre.
> 4. **Laisser l'utilisateur écrire le code critique** : Pour les algorithmes de graphe et la logique de détection, guider l'utilisateur pour qu'il écrive lui-même, en corrigeant au fur et à mesure. Claude Code peut écrire le boilerplate (connexions API, config, BDD), mais la logique métier doit être comprise et idéalement écrite par l'utilisateur.
> 5. **Analogies avec le C et Java** : L'utilisateur maîtrise le C et apprend Java. Quand un concept Python est nouveau, faire le parallèle avec ces langages.
> 6. **Pas de magie noire** : Si une librairie fait quelque chose de complexe sous le capot (web3.py, asyncio), prendre le temps d'expliquer ce qui se passe réellement.

---

## Phase 0 · Setup & fondations (1-2 jours)

- [ ] Créer le repo GitHub (README, .gitignore, LICENSE)
- [ ] Initialiser le projet Python avec structure modulaire :
  ```
  polymarket-insider-bot/
  ├── src/
  │   ├── chain/          # Monitoring on-chain (events, wallets)
  │   ├── clob/           # Interface API Polymarket CLOB
  │   ├── analysis/       # Détection de patterns & clustering
  │   ├── trading/        # Placement d'ordres (PRIVÉ, pas sur GitHub)
  │   └── alerts/         # Notifications Telegram/Discord
  ├── config/
  │   └── settings.py     # Seuils, paramètres
  ├── .env.example        # Template des variables d'environnement
  ├── requirements.txt
  └── README.md
  ```
- [ ] Installer les dépendances de base :
  - `web3.py`, `aiohttp`, `websockets`, `asyncio`
  - `networkx` (graphes), `sqlalchemy` (BDD)
  - `py-clob-client` (SDK Polymarket)
  - `python-telegram-bot` (alerting)
- [ ] Créer un compte Alchemy → obtenir clé API RPC Polygon
- [ ] Préparer un wallet Polygon (MetaMask ou généré via script)
- [ ] Configurer le fichier `.env` : clé RPC, clé privée wallet, token Telegram

> 🧠 **Point de réflexion — async/await** :
> Avant de continuer, pause. En C, tu gères la concurrence avec des threads ou du polling. En Python, `asyncio` propose un modèle différent : l'event loop.
> **Question** : D'après toi, pourquoi est-ce qu'un event loop asynchrone est plus adapté qu'un modèle multi-thread classique pour un bot qui écoute plusieurs sources de données (WebSocket blockchain + API Polymarket + commandes Telegram) en même temps ?
> *Discuter la réponse avant de passer à la Phase 1.*

---

## Phase 1 · Monitoring on-chain basique (3-5 jours)

**Objectif** : Détecter les nouveaux wallets qui déposent sur Polymarket.

- [ ] Identifier les adresses des smart contracts Polymarket (CTF Exchange, NegRisk, USDC sur Polygon)
- [ ] Se connecter au WebSocket Alchemy pour écouter les events `Transfer` USDC vers les contrats Polymarket

> 🧠 **Point de réflexion — Events & Logs** :
> Sur Ethereum/Polygon, les smart contracts émettent des "events" qui sont stockés dans les logs de transaction.
> **Question** : Le contrat USDC a une fonction `transfer(address to, uint256 amount)` qui émet un event `Transfer(address from, address to, uint256 value)`. Comment ferais-tu pour filtrer UNIQUEMENT les transfers dont la destination est le contrat Polymarket, sans télécharger tous les transfers USDC du réseau ?
> *Indice : pense aux "topics" des logs Ethereum et à comment les adresses sont encodées.*

- [ ] Pour chaque transfert détecté :
  - Récupérer l'adresse du wallet source
  - Vérifier l'ancienneté du wallet (première transaction via `eth_getTransactionCount` ou scan de l'historique)
  - Enregistrer en BDD : adresse, date de création estimée, montant déposé, timestamp

> 🧠 **Point de réflexion — Estimation d'ancienneté** :
> **Question** : `eth_getTransactionCount` te donne le nombre de transactions d'un wallet, pas sa date de création. Comment pourrais-tu estimer l'ancienneté d'un wallet de manière fiable ? Quels sont les tradeoffs entre précision et coût en appels RPC ?
> *Piste : pense à la recherche dichotomique sur les blocs, ou à l'utilisation d'un indexeur comme PolygonScan API.*

- [ ] Définir les premiers seuils d'alerte :
  - Wallet < 7 jours + dépôt > 5 000 USDC → alerte basique
  - Wallet < 1 jour + dépôt > 1 000 USDC → alerte haute
- [ ] Envoyer une notification Telegram avec les détails du wallet suspect

**Livrable** : Bot qui tourne et envoie des alertes Telegram quand un wallet suspect dépose sur Polymarket.

---

## Phase 2 · Intégration API CLOB Polymarket (3-5 jours)

**Objectif** : Suivre ce que font les wallets suspects après leur dépôt.

- [ ] Se connecter à l'API CLOB Polymarket (REST + WebSocket)
- [ ] Pour chaque wallet suspect détecté en Phase 1 :
  - Suivre ses ordres en temps réel (quel marché, quelle direction, quel montant)
  - Identifier le marché ciblé et la direction du pari (Yes/No)

> 🧠 **Point de réflexion — Architecture du pipeline** :
> Tu as maintenant deux flux de données en temps réel : les events on-chain ET les trades sur le CLOB.
> **Question** : Comment structurerais-tu la communication entre le module `chain/` (qui détecte les dépôts) et le module `clob/` (qui suit les trades) ? Dessine mentalement le flux de données. Est-ce que `clob/` doit écouter TOUS les trades, ou seulement ceux des wallets flaggés par `chain/` ? Quels sont les avantages et inconvénients de chaque approche ?

- [ ] Enrichir les alertes :
  - « Wallet 0xABC (créé il y a 2 jours) vient de miser 15 000 USDC sur YES sur le marché "Will X happen?" »
- [ ] Stocker l'historique des trades suspects en BDD

> 🧠 **Point de réflexion — Modélisation BDD** :
> **Question** : Quelles tables te faut-il ? Quelles sont les relations entre elles ? Dessine le schéma (wallets, trades, marchés, alertes, clusters...). Pense aux requêtes que tu voudras faire plus tard : "tous les trades suspects sur un marché donné", "tous les wallets liés à un cluster", etc.
> *Écris le schéma toi-même, on corrigera ensemble.*

- [ ] Créer un dashboard minimal (CLI ou simple page web) pour visualiser les wallets trackés

**Livrable** : Alertes enrichies avec le contexte du trade (marché + direction + montant).

---

## Phase 3 · Analyse de clusters (5-10 jours)

**Objectif** : Détecter les insiders qui fragmentent sur plusieurs wallets.

> 🧠 **Point de réflexion MAJEUR — Théorie des graphes appliquée** :
> C'est le cœur intellectuel du projet. Avant d'écrire une seule ligne de code :
>
> **Question 1 — Modélisation** : Tu dois représenter les flux financiers entre wallets sous forme de graphe. Est-ce que ton graphe est orienté ou non ? Pondéré ou non ? Pourquoi ? Qu'est-ce qu'un nœud, qu'est-ce qu'une arête ? Est-ce qu'il peut y avoir plusieurs arêtes entre deux nœuds (multigraphe) ?
>
> **Question 2 — Parcours** : Un insider finance 20 wallets via 3 niveaux d'intermédiaires (arbre). Tu connais un wallet suspect en bas de l'arbre. Comment remontes-tu jusqu'à la racine ? Quel algorithme de parcours utilises-tu et pourquoi ? Quelle est sa complexité ?
>
> **Question 3 — Clustering** : Tu as identifié 200 wallets qui ont interagi avec Polymarket dans la dernière heure. Comment détectes-tu lesquels forment un "groupe" ? Connais-tu des algorithmes de détection de communautés dans un graphe ? Si non, réfléchis à comment TU ferais, puis on regardera les algorithmes existants.
>
> *Prends le temps de réfléchir à ces 3 questions. Écris tes réponses, même imparfaites. C'est ici que tu apprends le plus.*

- [ ] Construire le graphe de transactions avec `networkx` :
  - Nœuds = wallets
  - Arêtes = transferts de fonds (USDC, MATIC)
  - Poids = montants transférés

> 🧠 **Point de réflexion — Implémentation** :
> **Exercice** : Écris toi-même la fonction `build_funding_graph(wallet_address, depth)` qui, à partir d'un wallet, construit le graphe de ses connexions financières sur N niveaux de profondeur. Utilise `networkx` et `web3.py`. Pense à :
> - Quelle condition d'arrêt pour ne pas explorer tout Polygon ?
> - Comment éviter les cycles ?
> - Comment gérer les wallets qui ont des milliers de transactions (exchanges, contrats populaires) ?
>
> *Écris un premier brouillon, on itérera ensemble.*

- [ ] Implémenter les heuristiques de clustering :
  - **Temporel** : N wallets qui tradent le même marché, même direction, dans une fenêtre de X minutes
  - **Funding** : wallets financés par la même source (directement ou via intermédiaires)
  - **Comportemental** : mêmes patterns (nonce bas, pas d'historique, montants similaires)
  - **Gas** : wallets financés en MATIC depuis la même adresse

> 🧠 **Point de réflexion — Scoring** :
> **Question** : Tu as 4 signaux différents (temporel, funding, comportemental, gas). Comment les combines-tu en un score unique de suspicion ? Est-ce que tous les signaux ont le même poids ? Comment évites-tu les faux positifs (un groupe d'amis qui misent ensemble légitimement) ?
> *Propose ta propre formule de scoring avant de coder.*

- [ ] Scoring de suspicion par cluster
- [ ] Alertes enrichies : « Cluster de 8 wallets détecté, tous financés par 0xDEF, total 80k USDC sur YES sur marché Z »

**Livrable** : Détection de clusters avec scoring et visualisation du graphe.

---

## Phase 4 · Module de copy-trading (5-7 jours)

> ⚠️ Ce module reste PRIVÉ — ne pas publier sur GitHub.

**Objectif** : Passer des ordres automatiquement quand un signal d'insider est détecté.

> 🧠 **Point de réflexion — Gestion du risque** :
> Avant de coder quoi que ce soit qui touche à de l'argent réel :
> **Question 1** : Tu détectes un insider avec un score de suspicion de 85/100 sur un marché où le prix est à 0.30 pour "Yes". Combien tu mises ? Pourquoi ? Qu'est-ce qui se passe si c'est un faux positif ?
> **Question 2** : Tu as 3 signaux d'insider actifs en même temps sur 3 marchés différents. Comment répartis-tu ton capital ? Qu'est-ce que la corrélation entre ces marchés change à ta stratégie ?
> **Question 3** : Connais-tu le critère de Kelly ? Pourquoi est-il pertinent ici ?
> *Réfléchis à ces questions AVANT d'écrire le module de trading.*

- [ ] Intégrer `py-clob-client` pour le placement d'ordres
- [ ] Générer une API key CLOB via signature du wallet
- [ ] Implémenter la logique de trading :
  - Taille de position : % du bankroll proportionnel au score de suspicion
  - Type d'ordre : limit order légèrement au-dessus du prix actuel
  - Délai max : si le prix a déjà bougé de X% depuis la détection, ne pas entrer
- [ ] Gestion du risque :
  - Exposition max par marché
  - Exposition max totale
  - Stop-loss si le prix va dans le mauvais sens
  - Cooldown après une perte
- [ ] Logging complet de chaque trade (entrée, sortie, P&L)

> 🧠 **Point de réflexion — Paper trading d'abord** :
> **Règle non négociable** : le bot tourne en mode SIMULATION pendant au moins 2 semaines avant de toucher à de l'argent réel. Pendant cette période, logge chaque trade que le bot AURAIT passé et calcule le P&L théorique. Ça te permet de valider ta stratégie sans risque.

**Livrable** : Bot qui copy-trade automatiquement les insiders détectés.

---

## Phase 5 · Optimisation & production (continu)

- [ ] Backtester la stratégie sur des données historiques
- [ ] Affiner les seuils (ancienneté wallet, montant min, taille cluster, fenêtre temporelle)
- [ ] Monitorer les faux positifs / faux négatifs

> 🧠 **Point de réflexion — Métriques** :
> **Question** : Comment mesures-tu la performance de ton détecteur ? Qu'est-ce qu'un "vrai positif" dans ton contexte — comment le vérifies-tu après coup ? Quelles métriques (précision, rappel, F1) sont les plus importantes pour toi et pourquoi ?

- [ ] Migrer sur VPS ou Raspberry Pi pour du 24/7
- [ ] Ajouter un dashboard web (Flask/FastAPI + simple frontend)
- [ ] Publier la partie détection sur GitHub avec un bon README + schémas d'architecture

---

Phase BONUS · Arbitrage d'information — News Front-Running

⚠️ Ce module est indépendant de la détection d'insiders. C'est une stratégie complémentaire.

Objectif : Détecter le résultat d'un événement via des sources externes avant que le prix Polymarket ne converge, et placer un ordre dans la fenêtre de latence.
Principe : Quand un événement se produit (résultat sportif, annonce officielle, décision gouvernementale), il y a un délai entre le moment où l'information est publique et le moment où le marché Polymarket reflète le résultat à 95%+. Ce délai peut aller de quelques secondes à plusieurs minutes. Le but est d'être dans cette fenêtre.
🧠 CHECKPOINT RÉFLEXION #11 — Latence et sources d'information

Claude Code : Demande à l'étudiant :

« Quelles sources d'information publient un résultat avant que Polymarket ne réagisse ? Pense par catégorie de marché (sport, politique, tech, géopolitique). »
« Quelle est la chaîne complète de latence : événement réel → source publique → ton bot → ordre exécuté ? Où sont les goulots d'étranglement ? »
« Comment tu détermines automatiquement si une news correspond à un marché Polymarket ouvert ? C'est un problème de NLP — comment tu l'aborderais ? »

C'est un problème d'ingénierie en temps réel. La vitesse est tout.


 Identifier les sources de données par catégorie de marché :

Sport : APIs temps réel (ESPN, SportRadar, odds APIs)
Politique/gouvernemental : flux RSS AP/Reuters, comptes X officiels, APIs gouvernementales
Tech : blogs officiels (OpenAI, Google), comptes X des CEOs, Product Hunt
Géopolitique : feeds OSINT, alertes militaires, trafic aérien (ADS-B)
Crypto : APIs on-chain, annonces de protocoles, governance votes


 Construire un listener multi-sources asynchrone (WebSocket + polling rapide)
 Mapper automatiquement les news entrantes aux marchés Polymarket ouverts

Matching par mots-clés / entités nommées
Vérifier que le marché n'a pas déjà convergé (prix < 0.90 pour être intéressant)


 Logique de trading ultra-rapide :

Pré-calculer les ordres pour les marchés surveillés (montant, direction)
Exécuter immédiatement quand le signal est confirmé
Timeout strict : si le prix a déjà bougé de X%, annuler


 Gestion du risque spécifique :

Ne jamais trader sur un signal ambigu (la news doit confirmer clairement le résultat)
Position sizing conservateur (la fenêtre est courte, le risque d'erreur existe)
Blacklist de marchés trop compétitifs (élections US, gros marchés crypto) où les bots pros sont déjà dessus



🧠 CHECKPOINT RÉFLEXION #12 — Éthique et limites

Claude Code : Demande à l'étudiant :

« Quelle est la différence entre ce qu'on fait ici (arbitrage d'information publique) et l'insider trading ? Où est la ligne ? »
« Est-ce que cette stratégie reste profitable si tout le monde fait la même chose ? Qu'est-ce que ça implique sur le long terme ? »
« Quels sont les risques techniques ? (faux signal, API down, ordre partiellement rempli, flash crash du prix) »


Livrable : Module complémentaire qui exploite la latence entre les sources de news et les prix Polymarket.
Note : Ce module est probablement le plus compétitif — des équipes avec des infras dédiées font déjà ça. Ton avantage sera sur les marchés niche à faible volume où personne n'a automatisé le monitoring. Commence par une catégorie (ex: annonces tech) et étends ensuite.

## Stack technique récapitulatif

| Composant | Outil |
|---|---|
| Langage | Python 3.11+ |
| Monitoring on-chain | web3.py + Alchemy WebSocket (Polygon) |
| API Polymarket | py-clob-client + aiohttp |
| Analyse de graphe | networkx → Neo4j si scale |
| Base de données | SQLite (dev) → PostgreSQL (prod) |
| Alerting | python-telegram-bot |
| Orchestration | asyncio (event loop unique) |
| IDE | VS Code |
| Déploiement | Local → Raspberry Pi ou VPS |

---

## Ressources utiles

- [Polymarket CLOB API docs](https://docs.polymarket.com)
- [py-clob-client GitHub](https://github.com/Polymarket/py-clob-client)
- [Alchemy Polygon WebSocket](https://docs.alchemy.com/reference/polygon-api-quickstart)
- [web3.py docs](https://web3py.readthedocs.io/)
- [networkx docs](https://networkx.org/)
- [PolygonScan](https://polygonscan.com/) — pour explorer les contrats Polymarket manuellement
