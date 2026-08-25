@echo off
REM ---------------------------------------------------------------------------
REM  PrecoBanana - modo portatil
REM  Duplo clique aqui. Nao precisa instalar nada nem ser administrador.
REM
REM  %~dp0 e' a pasta deste arquivo: por isso funciona com o pendrive em
REM  qualquer letra (E:, F:, G:...) sem nenhum ajuste.
REM ---------------------------------------------------------------------------
title PrecoBanana - Portal de Ofertas

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ferramentas\iniciar.ps1"

if errorlevel 1 (
    echo.
    echo Algo deu errado. A janela fica aberta para voce ler a mensagem acima.
    pause
)
