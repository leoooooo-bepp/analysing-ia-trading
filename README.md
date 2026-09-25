# analysing-ia-trading — Robot d'arbitrage crypto agressif

Robot Python asynchrone (ccxt / ccxt.pro) qui fait tourner **trois moteurs d'arbitrage** en parallèle
sur jusqu'à 8 plateformes, avec une configuration taillée pour maximiser le rendement.

| Moteur | Principe | Source du rendement |
|---|---|---|
| `cross_exchange` | Achat sur la plateforme A et vente sur B **en même temps**, sur inventaire pré-positionné (aucun transfert on-chain pendant le trade) | écarts de prix entre plateformes |
| `triangular` | USDT → X → Y → USDT sur une même plateforme, **3 jambes simultanées** financées par l'inventaire | incohérences entre paires croisées |
| `funding` | Cash-and-carry : long spot + short perpétuel, delta neutre, **levier 3x** sur la jambe perp | funding rate payé toutes les 8 h |

## Démarrage

```bash
pip install -r requirements.txt

# 1. Démo hors ligne (marché synthétique, aucune connexion requise)
python -m arbitrage_bot --mode demo --duration 120

# 2. Paper trading : vrais carnets d'ordres en temps réel, ordres simulés
python -m arbitrage_bot --mode paper

# 3. Live : ordres réels (clés dans .env, voir .env.example — JAMAIS le droit de retrait)
python -m arbitrage_bot --mode live
```

Tests : `python -m pytest`

## Notifications Telegram

Le robot t'envoie un message pour chaque trade exécuté : plateformes, sens, quantités, prix
moyens, profit réalisé vs attendu, puis un récapitulatif (capital, PnL du jour, nombre de trades,
PnL par stratégie). Tu reçois aussi le démarrage/arrêt, les entrées/sorties de cash-and-carry,
le kill switch et le disjoncteur.

```
✅ Arbitrage inter-plateformes — BTC/USDT
🟢 Achat binance 0.012300 BTC/USDT @ 65 010.50
🔴 Vente okx 0.012300 BTC/USDT @ 65 120.10
Engagé : 800.12 USDT · Écart : 0.168 %
Profit : +1.02 USDT (attendu +1.10)
━━━━━━━━━━
📊 Capital 10 045.20 USDT (+0.45 %)
Aujourd'hui : +45.20 USDT · 37 trades
Par stratégie : inter +12.30 · triangulaire +30.10 · funding +2.80
```

Mise en place (2 minutes) :
1. Sur Telegram, écris à **@BotFather** → `/newbot` → copie le jeton dans `TELEGRAM_BOT_TOKEN` (fichier `.env`).
2. Envoie un message quelconque à ton nouveau bot.
3. Ouvre `https://api.telegram.org/bot<TON_JETON>/getUpdates` dans un navigateur et copie
   `"chat":{"id": ...}` dans `TELEGRAM_CHAT_ID`.
4. Lance le robot : le fichier `.env` est chargé automatiquement.

Telegram limite l'envoi à ~1 message/s : quand plusieurs trades tombent en même temps, ils sont
regroupés dans un seul message. Pour ne recevoir que les trades significatifs, règle
`telegram.min_abs_pnl` (les échecs sont toujours notifiés).

## Ce qui rend la configuration agressive (`config/aggressive.yaml`)

1. **Frais au plancher** — paliers VIP + remises token natif (BNB, BGB, KCS). C'est le levier n°1 :
   chaque 0,01 % économisé est du rendement net sur *chaque* trade.
2. **Large couverture** — 8 plateformes × 15 paires, dont des mid-caps (SUI, PEPE, WIF, INJ…) où les
   écarts sont plus larges et la concurrence HFT plus faible que sur BTC/ETH.
3. **Seuils serrés** — déclenchement dès 0,05 % net après frais (0,04 % en triangulaire).
4. **Profondeur exploitée** — le robot parcourt le carnet niveau par niveau et prend *toute* la taille
   rentable, pas seulement le meilleur prix.
5. **Gros tickets + intérêts composés** — 35 % du capital par trade, taille recalculée sur le capital courant.
6. **Levier sur le carry** — 3x sur la jambe perp : le capital immobilisé passe de 2× à 1,33× le notionnel.
7. **Latence minimale** — WebSockets, 10 scans/s, jambes envoyées en parallèle.
8. **Exécution défensive** — ordres IOC à prix limite ; une jambe orpheline est d'abord complétée au
   prix d'équilibre, puis couverte en agressif si le marché s'est enfui.

L'agressivité porte sur l'exposition, pas sur l'absence de filet : **kill switch à −6 %/jour**,
disjoncteur après 6 échecs consécutifs (reprise après 30 s), plafond de positions de carry.

## Ce que montrent les simulations

Le mode démo modélise volontairement la réalité du terrain : les autres arbitragistes referment
les écarts en permanence et le carnet bouge entre la détection et l'arrivée de l'ordre. Résultat
typique sur 2 minutes : PnL proche de zéro / légèrement négatif, beaucoup d'opportunités « ratées »
(sans coût) et quelques couvertures en perte. Le carry funding ne paie que ses frais d'entrée
sur une démo courte : il se rentabilise sur plusieurs jours.

La conclusion est celle du métier : **en arbitrage, le rendement se gagne sur la latence et les frais**,
pas sur l'agressivité des seuils. D'où les prochaines étapes pour aller plus loin :

- **Hébergement co-localisé** : VPS à Tokyo (Binance/OKX/Bybit) → latence de ~200 ms à < 5 ms.
- **Jambe maker** : poser un ordre limite passif sur la plateforme la moins liquide (frais maker
  nuls ou rebates chez MEXC/Bybit) et ne couvrir en taker qu'une fois rempli.
- **Funding multi-plateformes** : short le perp là où le funding est le plus élevé, long là où il est le plus bas.
- Valider chaque changement en `paper` pendant plusieurs jours avant tout passage en `live`.

## Architecture

```
arbitrage_bot/
  config.py          dataclasses + chargement YAML
  orderbook.py       VWAP et taille rentable maximale en profondeur
  strategies/        cross_exchange, triangular, funding
  risk.py            sizing, kill switch, disjoncteur
  execution.py       ordres IOC parallèles, complétion/couverture des jambes
  exchanges.py       ccxt (live/paper) + marché synthétique (demo)
  rebalancer.py      rééquilibrage de l'inventaire entre plateformes
  engine.py          boucle principale, flux de carnets, suivi PnL
  notifier.py        notifications Telegram (file d'attente, regroupement, limite de débit)
config/aggressive.yaml
tests/
```
