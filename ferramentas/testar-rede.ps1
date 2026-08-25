# Roda o check_proxy.py com o Python e o Firefox embarcados no pendrive.
# Mesmo caminho de codigo dos scrapers: o que der aqui e' o que vai dar la.

$ErrorActionPreference = "Stop"
$Raiz = Split-Path -Parent $PSScriptRoot

$python      = Join-Path $Raiz "runtime\python\python.exe"
$pastaPortal = Join-Path $Raiz "app\web_scraping"
$browsers    = Join-Path $Raiz "runtime\browsers"

if (-not (Test-Path $python)) {
    Write-Host "  ERRO: pacote incompleto (runtime\python\python.exe nao existe)." -ForegroundColor Red
    exit 1
}

$env:PLAYWRIGHT_BROWSERS_PATH = $browsers
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

Write-Host ""
Write-Host "  Testando esta rede. Leva cerca de 1 minuto..." -ForegroundColor Cyan
Write-Host ""

Push-Location $pastaPortal
try {
    & $python "check_proxy.py"
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "  Como ler o resultado:" -ForegroundColor Cyan
Write-Host "    tres [v]         -> pode usar, a busca vai funcionar aqui"
Write-Host "    IP = DATACENTER  -> rede de empresa/servidor; a busca vai voltar vazia"
Write-Host "    ML ou Amazon [x] -> esta rede esta marcada; tente de outra casa"
Write-Host ""
