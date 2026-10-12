# Abre o Firefox do programa para entrar na conta do Mercado Livre (uma vez).
# A sessao salva e' usada so pelo cacador de cupons; as buscas seguem anonimas.

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "comum.ps1")

$Raiz        = Split-Path -Parent $PSScriptRoot
$pastaPortal = Join-Path $Raiz "app\web_scraping"
$browsers    = Join-Path $Raiz "runtime\browsers"

# Mesmo Python que o INICIAR.bat usa (portatil, .venv ou do sistema).
$python = Join-Path $Raiz "runtime\python\python.exe"
if (-not (Test-Path $python)) {
    $venvRaiz = Join-Path $Raiz ".venv\Scripts\python.exe"
    $venvApp  = Join-Path $pastaPortal ".venv\Scripts\python.exe"
    if (Test-Path $venvRaiz) { $python = $venvRaiz }
    elseif (Test-Path $venvApp) { $python = $venvApp }
    else {
        $cmdPy = Get-Command "python.exe" -ErrorAction SilentlyContinue
        if ($cmdPy) { $python = $cmdPy.Source }
    }
}
if (-not $python -or -not (Test-Path $python)) {
    Write-Host "  ERRO: nao encontrei o Python do programa." -ForegroundColor Red
    exit 1
}

if (Test-Path $browsers) { $env:PLAYWRIGHT_BROWSERS_PATH = $browsers }
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

Write-Host ""
Write-Host "  LOGIN DO MERCADO LIVRE (cacador de cupons)" -ForegroundColor Yellow

Push-Location $pastaPortal
try {
    & $python "login_mercadolivre.py"
    $codigo = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $codigo
