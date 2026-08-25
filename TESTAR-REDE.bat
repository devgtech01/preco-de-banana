@echo off
REM ---------------------------------------------------------------------------
REM  Descobre, em ~1 minuto, se ESTA rede serve para buscar ofertas.
REM
REM  Responde tres coisas: por qual IP voce esta saindo, se esse IP e' visto
REM  como residencial ou de datacenter, e se Mercado Livre e Amazon deixam
REM  entrar. Rode isto ao chegar num computador novo, antes de tentar buscar.
REM ---------------------------------------------------------------------------
title PrecoBanana - Testando a rede

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ferramentas\testar-rede.ps1"

pause
