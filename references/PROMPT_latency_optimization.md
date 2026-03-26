# Stratégie d'arbitrage post-résolution — Optimisation latence

## Contexte

Le bot exploite les ordres stale laissés par les market makers après la résolution d'un marché Polymarket. Quand un marché se resolve, les MM qui n'ont pas annulé leurs ordres à temps ont des ordres dans le book à des prix obsolètes. Le bot achète/vend contre ces ordres et redeem pour le profit.

C'est une course de vitesse pure — le premier bot à atteindre les ordres stale prend tout.

## Infrastructure actuelle

- VPS : Manchester, UK
- Nœud RPC : Chainstack privé, Londres
- CLOB Polymarket : AWS eu-west-2 (Londres)
- Langage : Python
- Client CLOB : py-clob-client

## Chaîne de latence complète

```
Résolution UMA on-chain
    → Nœud Chainstack détecte l'event (~100-300ms)
    → Bot traite le signal + décide d'agir (~20-50ms en Python)
    → Ordre envoyé au CLOB (~2-4ms Manchester → Londres)
    → CLOB matche l'ordre (~1ms côté serveur)
    = Total estimé : ~125-355ms
```

Chaque maillon est un levier d'optimisation. Voici les gains possibles, par ordre d'impact.

## Optimisations à implémenter

### 1. WebSocket permanent + book en mémoire (impact : -50 à -200ms)

Ne PAS faire de requête REST pour lire le book au moment de la résolution. Maintenir un WebSocket ouvert en permanence sur les marchés surveillés :
- `wss://ws-subscriptions-clob.polymarket.com/ws/` pour le market channel
- Mettre à jour le book en mémoire à chaque event
- Au moment de la résolution, le book est déjà là — zéro fetch nécessaire

C'est le gain le plus important. Un aller-retour REST c'est ~10-50ms minimum. Avec le book en mémoire, c'est 0ms.

### 2. Pré-computation des ordres (impact : -10 à -30ms)

Pour chaque marché surveillé, pré-calculer en avance :
- Quels ordres stale dans le book seraient profitables si le marché resolve YES vs NO
- Le montant optimal à trader (respecter la limite 2% du volume 24h)
- Pré-construire l'objet ordre (token_id, prix, taille, side)

Au moment de la résolution, il ne reste qu'à signer et envoyer — pas de calcul.

### 3. Pré-signature des ordres EIP-712 (impact : -5 à -15ms)

Les ordres Polymarket sont des messages EIP-712 signés cryptographiquement. La signature prend quelques ms. Deux options :
- Pré-signer les deux scénarios (resolve YES et resolve NO) et envoyer le bon
- Ou utiliser un signer optimisé (le client Rust officiel supporte AWS KMS et les remote signers)

Attention : les ordres pré-signés ont un salt/nonce — vérifier qu'ils ne sont pas invalidés entre la pré-signature et l'envoi.

### 4. Connection keep-alive (impact : -50 à -100ms sur premier ordre)

Le handshake TLS + authentification HMAC ne doit se faire qu'une seule fois. Maintenir la connexion HTTP ouverte en permanence vers `clob.polymarket.com`. Si la connexion drop et se recrée, on perd 50-100ms de handshake.

En Python avec `aiohttp`, utiliser une session persistante :
```python
session = aiohttp.ClientSession()  # créer une fois, réutiliser toujours
```

Ne jamais faire `async with aiohttp.ClientSession() as session:` dans le hot path.

### 5. Client Rust polyfill-rs (impact : -15 à -40ms sur traitement)

`floor-licker/polyfill-rs` est un client Rust Polymarket ultra-optimisé :
- Zéro allocation dans les hot paths (pools pré-alloués)
- Structures de données groupées pour l'efficacité des lignes de cache
- Prix en virgule fixe, arithmétique entière, opérations sans branchement
- Drop-in replacement pour le client Rust officiel

Si on migre le hot path (détection résolution → envoi ordre) en Rust, le traitement passe de ~20-50ms (Python) à <1ms (Rust).

Architecture cible : Python gère le monitoring, l'analyse, la sélection des marchés. Un binaire Rust minimaliste gère uniquement le chemin critique (event résolution → ordre). Communication via socket local ou Redis.

### 6. Migration VPS Manchester → Dublin (impact : -2 à -4ms par requête)

Manchester-Londres : ~2-4ms RTT. Dublin-Londres : <1ms RTT. Sur un cycle avec 2-3 requêtes réseau, ça fait 4-12ms de gain. Dublin est recommandé plutôt que Londres car le UK est géo-bloqué par Polymarket — Dublin (eu-west-1) est le plus proche sans restriction.

## Notes techniques Polymarket

- Le rate limiting utilise le throttling Cloudflare (retardé, pas rejeté). Burst autorisé sur le trading.
- Les marchés courts (15 min, crypto) utilisent Chainlink Data Streams au lieu de UMA — résolution automatique, plus rapide.
- Les marchés longs utilisent UMA Optimistic Oracle — fenêtre de challenge de ~2h, mais la résolution initiale (proposal) est détectable on-chain immédiatement.
- Cloudflare protège les endpoints POST du CLOB — certaines IPs sont bloquées. Le client polyfill-rs et le SDK Rust officiel gèrent les retries automatiques.

## Repos de référence

- `Polymarket/rs-clob-client` — Client Rust officiel, heartbeat auto, remote signers
- `floor-licker/polyfill-rs` — Client Rust optimisé latence, zéro-alloc hot paths
- `Polymarket/py-clob-client` — Client Python actuel (à garder pour les parties non-critiques)

## Budget latence cible après optimisation

```
Résolution UMA on-chain
    → Nœud Chainstack détecte l'event (~100-200ms)
    → Rust traite le signal + envoie l'ordre pré-signé (<1ms)
    → Réseau Dublin → Londres (<1ms)
    → CLOB matche l'ordre (~1ms)
    = Total cible : ~103-203ms
```

Gain estimé vs setup actuel : **50-150ms**, ce qui peut faire la différence entre capter un ordre stale et arriver trop tard.

## Ordre d'implémentation

1. WebSocket permanent + book en mémoire → faisable immédiatement en Python
2. Pré-computation des ordres → faisable immédiatement en Python
3. Connection keep-alive → vérifier le code actuel, corriger si nécessaire
4. Migration VPS → décision infra, pas de code
5. Hot path en Rust (polyfill-rs) → projet séparé, plus long
6. Pré-signature → à implémenter avec le module Rust

Commencer par 1-2-3 en Python, mesurer les résultats, puis décider si Rust se justifie.
