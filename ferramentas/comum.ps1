# Funcoes compartilhadas pelo iniciar.ps1 e pelo parar.ps1.
# Nada aqui depende da letra do pendrive: tudo sai de $PSScriptRoot.

function Get-RaizPacote {
    # ferramentas\ -> raiz do pendrive
    Split-Path -Parent $PSScriptRoot
}

function Get-Config {
    param([string]$Raiz)

    $padrao = @{
        PORTA_PORTAL          = 5000
        PORTA_BOT             = 3005
        ABRIR_NAVEGADOR       = 1
        SOMENTE_ESTA_MAQUINA  = 1
        PEDIR_SENHA           = 0
    }

    $arquivo = Join-Path $Raiz "config.txt"
    if (-not (Test-Path $arquivo)) { return $padrao }

    foreach ($linha in (Get-Content $arquivo)) {
        $texto = $linha.Trim()
        if ($texto -eq "" -or $texto.StartsWith("#")) { continue }
        $par = $texto.Split("=", 2)
        if ($par.Count -eq 2) {
            $chave = $par[0].Trim()
            $valor = $par[1].Trim()
            if ($padrao.ContainsKey($chave)) { $padrao[$chave] = $valor }
        }
    }
    return $padrao
}

function Test-PortaEmUso {
    param([int]$Porta, [string]$Interface = "127.0.0.1")

    $ip = [System.Net.IPAddress]::Parse($Interface)
    $ouvinte = New-Object System.Net.Sockets.TcpListener($ip, $Porta)
    try {
        $ouvinte.Start()
        $ouvinte.Stop()
        return $false
    } catch {
        return $true
    }
}

function Get-PortaLivre {
    param([int]$Preferida, [string]$Interface = "127.0.0.1")

    # Windows reserva faixas de porta para o Hyper-V/WSL e a 5000 costuma cair
    # dentro delas em algumas maquinas. Em vez de falhar, anda para a proxima.
    for ($p = $Preferida; $p -lt ($Preferida + 25); $p++) {
        if (-not (Test-PortaEmUso -Porta $p -Interface $Interface)) { return $p }
    }
    throw "Nenhuma porta livre entre $Preferida e $($Preferida + 25)."
}

function Wait-Servico {
    param([string]$Url, [int]$Segundos = 90, [switch]$MostrarProgresso)

    # Medido num SanDisk USB 3.0 (FAT32): o portal leva ~67s para subir a frio,
    # contra ~5s no SSD — o Python precisa ler numpy, pandas e flask inteiros do
    # pendrive. Num pendrive USB 2.0 antigo isso multiplica. Por isso a espera e'
    # generosa e mostra progresso: uma linha parada por um minuto faz o usuario
    # achar que travou e arrancar a midia no meio da escrita.
    $inicio = Get-Date
    $limite = $inicio.AddSeconds($Segundos)
    $ultimoPonto = 0

    while ((Get-Date) -lt $limite) {
        if ($MostrarProgresso) {
            $decorrido = [int]((Get-Date) - $inicio).TotalSeconds
            if ($decorrido -ge ($ultimoPonto + 5)) {
                $ultimoPonto = $decorrido
                Write-Host "." -NoNewline
            }
        }
        try {
            # HttpWebRequest com Proxy nulo em vez de Invoke-WebRequest: se a
            # maquina anfitria tiver proxy configurado no Windows, o cmdlet
            # tenta sair pelo proxy ate para 127.0.0.1 e o health check nunca
            # responde — o launcher desistiria de um servico que subiu bem.
            $req = [System.Net.HttpWebRequest]::Create($Url)
            $req.Timeout = 3000
            $req.Proxy = $null
            $resp = $req.GetResponse()
            $codigo = [int]$resp.StatusCode
            $resp.Close()
            if ($codigo -eq 200) { return $true }
        } catch {
            Start-Sleep -Milliseconds 700
        }
    }
    return $false
}

function Stop-ProcessosDoPacote {
    param([string]$Raiz, [switch]$Silencioso)

    $encerrados = 0
    $prefixo = $Raiz.ToLower()

    # 1) Os PIDs anotados pelo iniciar.ps1.
    #
    # O .pid viaja dentro do pendrive. Se a execucao anterior foi em OUTRO
    # computador (ou antes de um reboot), o numero gravado ali pode pertencer
    # hoje a um processo qualquer da maquina anfitria — e matar processo alheio
    # e' exatamente o que um programa de pendrive nao pode fazer. Por isso o PID
    # so vale se o executavel dele estiver dentro deste pacote.
    foreach ($nome in @("bot", "portal")) {
        $arquivo = Join-Path $Raiz "logs\$nome.pid"
        if (Test-Path $arquivo) {
            $processId = (Get-Content $arquivo -Raw).Trim()
            $proc = Get-Process -Id $processId -ErrorAction SilentlyContinue
            if ($proc) {
                $caminho = $null
                try { $caminho = $proc.Path } catch { $caminho = $null }
                $procNome = $proc.ProcessName.ToLower()
                if (($caminho -and $caminho.ToLower().StartsWith($prefixo)) -or ($procNome -in @("node", "python", "waitress"))) {
                    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
                    $encerrados++
                }
            }
            Remove-Item $arquivo -Force -ErrorAction SilentlyContinue
        }
    }

    # 2) Sobras. O Firefox do Playwright e' filho do Python, mas quando o Python
    #    morre no meio de uma raspagem ele fica orfao segurando arquivos do
    #    pendrive — e ai a ejecao segura falha. Filtrar por caminho garante que
    #    so morre o que saiu DESTE pacote: um Firefox pessoal do dono da maquina
    #    esta instalado em Arquivos de Programas e nao casa com o prefixo.
    try {
        $sobras = Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object { $_.ExecutablePath -and $_.ExecutablePath.ToLower().StartsWith($prefixo) }
        foreach ($p in $sobras) {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            $encerrados++
        }
    } catch {
        if (-not $Silencioso) { Write-Host "  (nao consegui varrer processos orfaos: $($_.Exception.Message))" }
    }

    return $encerrados
}
