# Encerra tudo que saiu deste pendrive — inclusive um Firefox de raspagem que
# tenha ficado orfao. Enquanto algum deles estiver vivo, o Windows recusa a
# ejecao segura ("o dispositivo esta em uso").

$ErrorActionPreference = "Continue"
. (Join-Path $PSScriptRoot "comum.ps1")

$Raiz = Split-Path -Parent $PSScriptRoot

Write-Host ""
Write-Host "  Encerrando o PrecoBanana ($Raiz)..." -ForegroundColor Cyan

$total = Stop-ProcessosDoPacote -Raiz $Raiz

if ($total -gt 0) {
    Write-Host "  $total processo(s) encerrado(s)." -ForegroundColor Green
} else {
    Write-Host "  Nada estava rodando." -ForegroundColor DarkGray
}

Write-Host "  Pode remover o pendrive com seguranca." -ForegroundColor Green
Write-Host ""
