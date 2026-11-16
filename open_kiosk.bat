@echo off
rem Double-click launcher: finds the Fire TV, connects and opens the kiosk URL.
chcp 65001 >nul
cd /d "%~dp0"
title Abrir piltover no Fire TV

set "PYTHON_CMD="
where py >nul 2>nul && set "PYTHON_CMD=py -3"
if not defined PYTHON_CMD (
    where python >nul 2>nul && set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
    echo Python nao encontrado. Instale em https://www.python.org/downloads/
    echo e marque "Add Python to PATH" durante a instalacao.
    echo.
    pause
    exit /b 1
)

%PYTHON_CMD% src\firetv_kiosk.py open
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
    echo Tudo certo. Pode fechar esta janela.
) else (
    echo Nao foi possivel abrir. Leia a mensagem acima.
)
pause
exit /b %EXIT_CODE%
