@echo off
title ScalperFlow - Gerando Executavel
color 0A
echo.
echo  =========================================
echo   ScalperFlow Bot - Build EXE
echo  =========================================
echo.

cd /d "%~dp0"

echo  Gerando executavel... aguarde.
echo.

pyinstaller ^
  --onefile ^
  --windowed ^
  --name "ScalperFlowBot" ^
  --icon NONE ^
  --add-data "gui_config.json;." ^
  --hidden-import customtkinter ^
  --hidden-import MetaTrader5 ^
  --hidden-import pandas ^
  --hidden-import numpy ^
  --collect-all customtkinter ^
  scalperflow_gui.py

echo.
if exist dist\ScalperFlowBot.exe (
    echo  =========================================
    echo   EXE gerado com sucesso!
    echo   Arquivo: dist\ScalperFlowBot.exe
    echo  =========================================
    explorer dist
) else (
    echo  ERRO: Falha ao gerar o executavel.
)
echo.
pause
