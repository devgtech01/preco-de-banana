@echo off
REM ---------------------------------------------------------------------------
REM  Entra na sua conta do Mercado Livre para o cacador de cupons ler a
REM  central de cupons. Rode uma vez (e de novo se o painel pedir).
REM  Pode rodar com o programa aberto.
REM ---------------------------------------------------------------------------
title PrecoBanana - Login Mercado Livre

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ferramentas\login-mercadolivre.ps1"

pause
