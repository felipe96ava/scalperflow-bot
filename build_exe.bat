@echo off
title ScalperFlow - Gerando Executavel
color 0A
echo.
echo  =========================================
echo   ScalperFlow Bot - Build EXE
echo  =========================================
echo.

cd /d "%~dp0"

echo  Gerando executavel via ScalperFlowBot.spec... aguarde.
echo.

pyinstaller ScalperFlowBot.spec --noconfirm
set BUILD_RESULT=%ERRORLEVEL%

echo.
if %BUILD_RESULT% NEQ 0 (
    echo  =========================================
    echo   ERRO: PyInstaller falhou ^(codigo %BUILD_RESULT%^).
    echo   O dist\ScalperFlowBot.exe pode estar
    echo   ausente ou desatualizado.
    echo  =========================================
) else if exist dist\ScalperFlowBot.exe (
    echo  =========================================
    echo   EXE gerado com sucesso!
    echo   Arquivo: dist\ScalperFlowBot.exe
    echo  =========================================
    explorer dist
) else (
    echo  =========================================
    echo   ERRO: PyInstaller retornou 0 mas o .exe
    echo   nao foi encontrado em dist\.
    echo  =========================================
)
echo.
pause
