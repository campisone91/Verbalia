@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1
title Verbalia - Installer Automatico

cd /d "%~dp0"

:: 1. Verifica presenza di Python >= 3.10
where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [INFO] Python non rilevato nel PATH. Tentativo di installazione automatica tramite winget...
    where winget >nul 2>&1
    if !ERRORLEVEL! EQU 0 (
        winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
        set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
    ) else (
        echo [ERRORE] Python 3.10+ non trovato e winget non disponibile. Installare Python da https://www.python.org
        exit /b 3
    )
)

:: 2. Avvia l'installer con GUI testuale (oppure passa argomenti come --all o --only ...)
python tools\installer.py %*
set "EXIT_CODE=%ERRORLEVEL%"

exit /b %EXIT_CODE%
