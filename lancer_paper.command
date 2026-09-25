#!/bin/bash
# Double-clic sur Mac (ou ./lancer_paper.command sous Linux) : installe, teste Telegram, lance le paper trading.
cd "$(dirname "$0")" || exit 1

PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then
    echo "[ERREUR] Python n'est pas installé. Installe-le depuis https://www.python.org/downloads/ puis relance ce fichier."
    read -r -p "Appuie sur Entrée pour fermer." _
    exit 1
fi

if [ ! -d .venv ]; then
    echo "[1/4] Création de l'environnement Python..."
    "$PY" -m venv .venv || { echo "[ERREUR] Création impossible : envoie ce message à Claude."; read -r _; exit 1; }
fi
source .venv/bin/activate

echo "[2/4] Installation des dépendances (la première fois peut prendre 1 à 2 minutes)..."
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt || { echo "[ERREUR] Installation échouée : envoie ce message à Claude."; read -r _; exit 1; }

if [ ! -f .env ]; then
    cp .env.example .env
    echo
    echo "[ACTION] Un fichier .env vient d'être créé. Remplis TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID, enregistre, puis relance ce fichier."
    open -e .env 2>/dev/null || ${EDITOR:-nano} .env
    read -r -p "Appuie sur Entrée pour fermer." _
    exit 0
fi

echo "[3/4] Test de Telegram..."
python -m arbitrage_bot --test-telegram || echo "[ATTENTION] Telegram ne fonctionne pas : vérifie le fichier .env. Le robot démarre quand même, sans notifications."

echo "[4/4] Démarrage du paper trading (vrais prix, ordres simulés). Ctrl+C pour arrêter."
echo
python -m arbitrage_bot --mode paper
read -r -p "Robot arrêté. Appuie sur Entrée pour fermer." _
