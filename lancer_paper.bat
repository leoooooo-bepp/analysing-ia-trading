@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Robot d'arbitrage - paper trading

where python >nul 2>nul
if errorlevel 1 (
    echo [ERREUR] Python n'est pas installe.
    echo Installe-le depuis https://www.python.org/downloads/ en cochant "Add python.exe to PATH", puis relance ce fichier.
    pause
    exit /b 1
)

if not exist .venv (
    echo [1/4] Creation de l'environnement Python...
    python -m venv .venv || goto :erreur
)
call .venv\Scripts\activate.bat

echo [2/4] Installation des dependances (la premiere fois peut prendre 1 a 2 minutes)...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt || goto :erreur

if not exist .env (
    copy .env.example .env >nul
    echo.
    echo [ACTION] Un fichier .env vient d'etre cree. Remplis TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID, enregistre, puis relance ce fichier.
    notepad .env
    pause
    exit /b 0
)

echo [3/4] Test de Telegram...
python -m arbitrage_bot --test-telegram
if errorlevel 1 (
    echo [ATTENTION] Telegram ne fonctionne pas : verifie le fichier .env. Le robot demarre quand meme, sans notifications.
)

echo [4/4] Demarrage du paper trading (vrais prix, ordres simules). Ferme cette fenetre ou fais Ctrl+C pour arreter.
echo.
python -m arbitrage_bot --mode paper
pause
exit /b 0

:erreur
echo [ERREUR] L'installation a echoue : copie le message ci-dessus et envoie-le a Claude.
pause
exit /b 1
