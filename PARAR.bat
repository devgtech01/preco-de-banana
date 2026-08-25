@echo off
REM ---------------------------------------------------------------------------
REM  Encerra tudo que este pendrive iniciou.
REM  RODE ISTO ANTES DE REMOVER O PENDRIVE: com o banco de dados aberto, tirar o
REM  pendrive na marra corrompe as configuracoes e a sessao do WhatsApp.
REM ---------------------------------------------------------------------------
title PrecoBanana - Encerrando

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ferramentas\parar.ps1"

REM Pausa de ~3s so para dar tempo de ler a mensagem antes da janela fechar.
REM ping em vez de timeout: o timeout aborta com erro quando a entrada esta
REM redirecionada (chamado por outro script, por exemplo).
ping -n 4 127.0.0.1 >nul 2>&1
