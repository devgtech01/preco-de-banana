# ---------------------------------------------------------------------------
# Sobe o bot (Node) e o portal (Python) usando SOMENTE o que esta no pendrive.
# Nada e' instalado na maquina anfitria e nada e' gravado fora desta pasta.
# ---------------------------------------------------------------------------

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "comum.ps1")

$Raiz = Split-Path -Parent $PSScriptRoot
$Logs = Join-Path $Raiz "logs"

Write-Host ""
Write-Host "  ================================" -ForegroundColor Yellow
Write-Host "   PRECO BANANA - modo portatil" -ForegroundColor Yellow
Write-Host "  ================================" -ForegroundColor Yellow
Write-Host "  rodando de: $Raiz" -ForegroundColor DarkGray
Write-Host ""

# --- 1. O pacote esta completo? -------------------------------------------
$pastaBot    = Join-Path $Raiz "app\bot-divulgar-produtos"
$pastaPortal = Join-Path $Raiz "app\web_scraping"
$browsers    = Join-Path $Raiz "runtime\browsers"

# 1.1 Localizar Python (portatil ou venv / sistema)
$python = Join-Path $Raiz "runtime\python\python.exe"
if (-not (Test-Path $python)) {
    $venvRaiz = Join-Path $Raiz ".venv\Scripts\python.exe"
    $venvApp  = Join-Path $pastaPortal ".venv\Scripts\python.exe"
    if (Test-Path $venvRaiz) {
        $python = $venvRaiz
    } elseif (Test-Path $venvApp) {
        $python = $venvApp
    } else {
        $cmdPy = Get-Command "python.exe" -ErrorAction SilentlyContinue
        if ($cmdPy) { $python = $cmdPy.Source }
    }
}

# 1.2 Localizar Node (portatil ou sistema)
$node = Join-Path $Raiz "runtime\node\node.exe"
if (-not (Test-Path $node)) {
    $cmdNode = Get-Command "node.exe" -ErrorAction SilentlyContinue
    if ($cmdNode) { $node = $cmdNode.Source }
}

foreach ($item in @($python, $node, (Join-Path $pastaBot "server.js"), (Join-Path $pastaPortal "servir.py"))) {
    if (-not $item -or -not (Test-Path $item)) {
        Write-Host "  ERRO: faltando $item" -ForegroundColor Red
        Write-Host "  Instale o Node.js e Python/venv ou monte a pasta runtime." -ForegroundColor Red
        exit 1
    }
}

if (-not (Test-Path $browsers)) {
    Write-Host "  Aviso: usando navegadores instalados no sistema (Playwright)." -ForegroundColor DarkGray
}

# --- 2. O pendrive aceita escrita? ----------------------------------------
try {
    New-Item -ItemType Directory -Force -Path $Logs | Out-Null
    $teste = Join-Path $Logs "escrita.tmp"
    Set-Content -Path $teste -Value "ok" -ErrorAction Stop
    Remove-Item $teste -Force
} catch {
    Write-Host "  ERRO: nao consigo gravar no pendrive." -ForegroundColor Red
    Write-Host "  Ele pode estar protegido contra gravacao (trave lateral) ou cheio." -ForegroundColor Red
    Write-Host "  O banco de configuracoes e a sessao do WhatsApp precisam gravar aqui." -ForegroundColor Red
    exit 1
}

# Restos de uma sessao anterior que nao foi encerrada pelo PARAR.bat.
$restos = Stop-ProcessosDoPacote -Raiz $Raiz -Silencioso
if ($restos -gt 0) { Write-Host "  Limpei $restos processo(s) de uma execucao anterior." -ForegroundColor DarkYellow }

# --- 3. Portas ------------------------------------------------------------
$cfg = Get-Config -Raiz $Raiz

$somenteLocal = ("$($cfg.SOMENTE_ESTA_MAQUINA)" -ne "0")
if ($somenteLocal) { $bind = "127.0.0.1" } else { $bind = "0.0.0.0" }

# O config.txt e' editado no Bloco de Notas por quem estiver com o pendrive na
# mao: um valor sem sentido nao pode derrubar o launcher com erro de conversao.
function Numero($valor, $padrao) {
    $n = 0
    if ([int]::TryParse("$valor", [ref]$n) -and $n -gt 0 -and $n -lt 65536) { return $n }
    return $padrao
}

$pedidoPortal = Numero $cfg.PORTA_PORTAL 5000
$pedidoBot    = Numero $cfg.PORTA_BOT    3005

# O teste de porta livre e' sempre no loopback: e' onde os dois servicos
# conversam entre si, independente de estarem ou nao abertos para a rede.
$portaPortal = Get-PortaLivre -Preferida $pedidoPortal
$portaBot    = Get-PortaLivre -Preferida $pedidoBot

if ($portaPortal -ne $pedidoPortal) {
    Write-Host "  Porta $pedidoPortal ocupada nesta maquina; usando $portaPortal." -ForegroundColor DarkYellow
}

# --- 4. Ambiente ----------------------------------------------------------
# Estas variaveis tem prioridade sobre os .env: tanto o python-dotenv quanto o
# dotenv do Node so preenchem o que ainda nao existe no ambiente. E' assim que
# a porta escolhida agora vence o valor gravado no arquivo.
if (Test-Path (Join-Path $Raiz 'runtime\node')) {
    $env:PATH = "$(Join-Path $Raiz 'runtime\node');$(Join-Path $Raiz 'runtime\python');$env:PATH"
} elseif ($python -and (Test-Path (Split-Path -Parent $python))) {
    $env:PATH = "$(Split-Path -Parent $python);$env:PATH"
}

$env:BIND_HOST   = $bind
$env:PORT        = "$portaBot"
$env:PORTAL_HOST = $bind
$env:PORTAL_PORT = "$portaPortal"

# Dispensa da tela de login. O portal e o bot so honram esta variavel se
# estiverem presos ao loopback — abrir para a rede reativa o login sozinho, do
# lado deles. Aqui ja nao ligamos quando $bind e' 0.0.0.0, para a mensagem na
# tela nao prometer o que nao vai acontecer.
$semLogin = ("$($cfg.PEDIR_SENHA)" -eq "0") -and $somenteLocal
if ($semLogin) { $env:SEM_LOGIN = "1" } else { $env:SEM_LOGIN = "0" }

# Sem isto o portal procuraria o bot no host "bot-service" (nome do container).
$env:BOT_API_URL = "http://127.0.0.1:$portaBot"

# Tira o Firefox de %LOCALAPPDATA% se a pasta embarcada existir no pendrive.
if (Test-Path $browsers) {
    $env:PLAYWRIGHT_BROWSERS_PATH = $browsers
} else {
    Remove-Item Env:\PLAYWRIGHT_BROWSERS_PATH -ErrorAction SilentlyContinue
}

$env:PYTHONIOENCODING = "utf-8"   # os logs tem emoji; sem isto o print quebra
$env:PYTHONUNBUFFERED = "1"       # log aparece na hora, nao em blocos
$env:TZ = "America/Sao_Paulo"

# Se a maquina anfitria usar proxy corporativo, a conversa portal <-> bot nao
# pode tentar sair por ele. (O SCRAPER_PROXY, que e' outra coisa, continua
# valendo normalmente para a raspagem.)
$env:NO_PROXY = "127.0.0.1,localhost"
$env:no_proxy = "127.0.0.1,localhost"

# --- 5. Sobe o bot --------------------------------------------------------
Write-Host "  Iniciando o bot (WhatsApp/Telegram)..." -NoNewline

Get-ChildItem $Logs -Filter "*.log" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue

$procBot = Start-Process -FilePath $node -ArgumentList "server.js" `
    -WorkingDirectory $pastaBot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $Logs "bot.log") `
    -RedirectStandardError  (Join-Path $Logs "bot.err.log")

Set-Content -Path (Join-Path $Logs "bot.pid") -Value $procBot.Id -Encoding ASCII

if (Wait-Servico -Url "http://127.0.0.1:$portaBot/api/health" -Segundos 240 -MostrarProgresso) {
    Write-Host " ok" -ForegroundColor Green
} else {
    Write-Host " FALHOU" -ForegroundColor Red
    Write-Host ""
    Get-Content (Join-Path $Logs "bot.err.log") -Tail 15 -ErrorAction SilentlyContinue
    Get-Content (Join-Path $Logs "bot.log") -Tail 15 -ErrorAction SilentlyContinue
    Stop-ProcessosDoPacote -Raiz $Raiz | Out-Null
    exit 1
}

# --- 6. Sobe o portal -----------------------------------------------------
Write-Host "  Iniciando o portal de ofertas (1a vez num PC novo demora ~1 min)..." -NoNewline

$procPortal = Start-Process -FilePath $python -ArgumentList "servir.py" `
    -WorkingDirectory $pastaPortal -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $Logs "portal.log") `
    -RedirectStandardError  (Join-Path $Logs "portal.err.log")

Set-Content -Path (Join-Path $Logs "portal.pid") -Value $procPortal.Id -Encoding ASCII

if (Wait-Servico -Url "http://127.0.0.1:$portaPortal/api/health" -Segundos 420 -MostrarProgresso) {
    Write-Host " ok" -ForegroundColor Green
} else {
    Write-Host " FALHOU" -ForegroundColor Red
    Write-Host ""
    Get-Content (Join-Path $Logs "portal.err.log") -Tail 20 -ErrorAction SilentlyContinue
    Stop-ProcessosDoPacote -Raiz $Raiz | Out-Null
    exit 1
}

# --- 7. Pronto ------------------------------------------------------------
$endereco = "http://127.0.0.1:$portaPortal"

Write-Host ""
Write-Host "  ------------------------------------------------------------" -ForegroundColor Green
Write-Host "   No ar: $endereco" -ForegroundColor Green
Write-Host "  ------------------------------------------------------------" -ForegroundColor Green

if ($semLogin) {
    Write-Host "   Entra direto, sem senha (so esta maquina alcanca o portal)."
} else {
    $usuario = "Admin"
    $envPortal = Join-Path $pastaPortal ".env.local"
    if (Test-Path $envPortal) {
        $linha = Select-String -Path $envPortal -Pattern "^PORTAL_USERNAME=(.*)$" -ErrorAction SilentlyContinue
        if ($linha) { $usuario = $linha.Matches[0].Groups[1].Value.Trim() }
    }
    Write-Host "   Usuario: $usuario   (senha: a mesma de sempre, guardada no pendrive)"
    if (-not $somenteLocal) {
        Write-Host "   Login exigido porque o portal esta aberto para a rede." -ForegroundColor DarkYellow
    }
}

if (-not $somenteLocal) {
    $ipLocal = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
        Select-Object -First 1).IPAddress
    if ($ipLocal) { Write-Host "   Do celular no mesmo wifi: http://$($ipLocal):$portaPortal" -ForegroundColor Yellow }
}

if ("$($cfg.ABRIR_NAVEGADOR)" -ne "0") { Start-Process $endereco }

Write-Host ""
Write-Host "   Deixe esta janela aberta enquanto usa." -ForegroundColor DarkGray
Write-Host "   Aperte Q para encerrar e liberar o pendrive." -ForegroundColor DarkGray
Write-Host ""

# --- 8. Segura a janela ---------------------------------------------------
# KeyAvailable estoura se a entrada estiver redirecionada (chamada por script,
# agendador, terminal embutido). Sem esta checagem a excecao cairia direto no
# finally e derrubaria os dois servicos um instante depois de subirem.
$interativo = $true
try { $null = [Console]::KeyAvailable } catch { $interativo = $false }
if (-not $interativo) {
    Write-Host "   (janela nao interativa: encerre pelo PARAR.bat)" -ForegroundColor DarkGray
}

try {
    while ($true) {
        if ($interativo -and [Console]::KeyAvailable) {
            $tecla = [Console]::ReadKey($true)
            if ($tecla.Key -eq "Q") { break }
        }
        # Sumico do .pid significa que quem encerrou foi o PARAR.bat — e nao uma
        # queda. Sem essa distincao a janela acusava "o bot caiu" toda vez que o
        # usuario encerrava do jeito certo.
        if ($procBot.HasExited) {
            if (Test-Path (Join-Path $Logs "bot.pid")) {
                Write-Host "  O bot caiu. Ultimas linhas do log:" -ForegroundColor Red
                Get-Content (Join-Path $Logs "bot.err.log") -Tail 10 -ErrorAction SilentlyContinue
            } else {
                Write-Host "  Encerrado pelo PARAR.bat." -ForegroundColor DarkGray
            }
            break
        }
        if ($procPortal.HasExited) {
            if (Test-Path (Join-Path $Logs "portal.pid")) {
                Write-Host "  O portal caiu. Ultimas linhas do log:" -ForegroundColor Red
                Get-Content (Join-Path $Logs "portal.err.log") -Tail 10 -ErrorAction SilentlyContinue
            } else {
                Write-Host "  Encerrado pelo PARAR.bat." -ForegroundColor DarkGray
            }
            break
        }
        Start-Sleep -Milliseconds 500
    }
} finally {
    Write-Host ""
    Write-Host "  Encerrando..." -NoNewline
    Stop-ProcessosDoPacote -Raiz $Raiz | Out-Null
    Write-Host " pronto. Pode remover o pendrive com seguranca." -ForegroundColor Green
    Start-Sleep -Seconds 2
}
