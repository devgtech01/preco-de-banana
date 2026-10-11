"""
Piloto automático: busca, escolhe e publica ofertas sozinho.

Roda dentro do processo do portal (servir.py liga uma thread em segundo plano),
então sobe com o INICIAR.bat e morre com o PARAR.bat sem processo extra. Usa o
mesmo SCRAPE_LOCK das buscas manuais: nunca abre dois Firefox ao mesmo tempo.

Um ciclo:
  1. pega a próxima categoria do rodízio (ordem de prioridade do categories.py);
  2. busca cada termo dela em cada loja de PILOTO_LOJAS;
  3. grava o preço de tudo que viu no histórico (data/piloto.db);
  4. dá nota a cada produto e escolhe os melhores, respeitando os limites;
  5. publica pelo bot (/api/post-deal), que aplica o link de afiliado da loja;
  6. dorme um intervalo aleatório entre PILOTO_INTERVALO_MIN e _MAX minutos.

Por que guardar histórico: "de R$ 999 por R$ 499" é muitas vezes um preço
"de" inflado. Depois de ver o mesmo produto algumas vezes, o piloto compara o
preço de hoje com a mediana do que ele mesmo observou e só confia no desconto
anunciado quando o preço está de fato no menor patamar já visto.

As regras ficam no config.txt (seção "Piloto automático") e são relidas a cada
ciclo: dá para mudar sem reiniciar.
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import sqlite3
import statistics
import sys
import threading
import time
import traceback
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlparse, urlunparse

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.abspath(os.path.join(BASE_DIR, "..", ".."))
CONFIG_TXT = os.path.join(RAIZ, "config.txt")
DB_PATH = os.path.join(BASE_DIR, "data", "piloto.db")
LOG_PATH = os.path.join(RAIZ, "logs", "piloto.log")

PADROES = {
    "PILOTO_ATIVO": "1",
    "PILOTO_INTERVALO_MIN": "10",
    "PILOTO_INTERVALO_MAX": "20",
    "PILOTO_LOJAS": "mercadolivre,amazon,aliexpress,kabum",
    "PILOTO_CATEGORIAS": "todas",
    "PILOTO_CATEGORIAS_POR_CICLO": "1",
    "PILOTO_ITENS_POR_LOJA": "8",
    "PILOTO_POSTS_POR_CICLO": "4",
    "PILOTO_MAX_POR_HORA": "15",
    "PILOTO_MAX_POR_LOJA_NO_CICLO": "2",
    "PILOTO_DESCONTO_MIN": "15",
    "PILOTO_PRECO_MIN": "15",
    "PILOTO_SILENCIO": "23-7",
    "PILOTO_REPOSTAR_DIAS": "3",
    "PILOTO_QUEDA_PARA_REPOSTAR": "5",
}

# Desconto acima disso quase sempre é erro de cadastro ou preço "de" fictício.
DESCONTO_SUSPEITO = 0.85

# Lojas cujo "de/por" costuma ser inflado de forma permanente. Enquanto não há
# histórico, o desconto anunciado delas pesa menos na nota (não no texto).
CONFIANCA_ANUNCIADO = {"aliexpress": 0.6}

# Observações mínimas para o histórico passar a valer mais que o "de/por".
MIN_OBSERVACOES = 3
JANELA_HISTORICO_DIAS = 30

# Palavras de acessório: "capa iphone 15" não é a oferta de "iphone 15".
ACESSORIOS = {
    "capa", "capinha", "case", "pelicula", "adesivo", "skin", "suporte", "cabo",
    "carregador", "fonte", "bateria", "controle", "tampa", "protetor", "bolsa",
    "mochila", "kit", "pecas", "peca", "reparo", "tela", "display", "dock",
    "base", "cooler", "pulseira", "alca", "estojo", "refil", "adaptador",
}
PALAVRAS_VAZIAS = {"de", "da", "do", "para", "com", "sem", "e", "a", "o", "em", "por"}

_estado = {
    "rodando": False,
    "fase": "parado",
    "detalhe": "",
    "ciclo": 0,
    "ultimo_ciclo": None,
    "proximo_ciclo": None,
    "ultima_categoria": None,
    "ultimo_erro": None,
}
_trava_estado = threading.Lock()
_acordar = threading.Event()
_thread: threading.Thread | None = None


# ---------------------------------------------------------------------------
# Configuração e log
# ---------------------------------------------------------------------------
def ler_config() -> dict:
    """config.txt tem prioridade sobre o padrão; variável de ambiente sobre ambos."""
    cfg = dict(PADROES)
    try:
        with open(CONFIG_TXT, encoding="utf-8-sig") as arquivo:
            for linha in arquivo:
                texto = linha.strip()
                if not texto or texto.startswith("#") or "=" not in texto:
                    continue
                chave, valor = texto.split("=", 1)
                chave = chave.strip()
                if chave in cfg:
                    cfg[chave] = valor.strip()
    except OSError:
        pass
    for chave in cfg:
        if os.getenv(chave):
            cfg[chave] = os.getenv(chave)
    return cfg


def _num(cfg: dict, chave: str, minimo: float = 0) -> float:
    try:
        return max(minimo, float(str(cfg.get(chave, "")).replace(",", ".")))
    except ValueError:
        return max(minimo, float(PADROES[chave]))


def log(msg: str) -> None:
    linha = f"[{_agora():%d/%m %H:%M:%S}] 🤖 {msg}"
    print(linha, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as arquivo:
            arquivo.write(linha + "\n")
    except OSError:
        pass


def _fase(fase: str, detalhe: str = "") -> None:
    with _trava_estado:
        _estado["fase"] = fase
        _estado["detalhe"] = detalhe


# ---------------------------------------------------------------------------
# Banco: histórico de preços, postagens e estado do rodízio
# ---------------------------------------------------------------------------
def conectar(caminho: str | None = None) -> sqlite3.Connection:
    caminho = caminho or DB_PATH
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    con = sqlite3.connect(caminho, timeout=30)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS precos (
            chave TEXT NOT NULL,
            preco REAL NOT NULL,
            visto_em TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_precos_chave ON precos(chave, visto_em);
        CREATE TABLE IF NOT EXISTS postados (
            chave TEXT NOT NULL,
            plataforma TEXT,
            titulo TEXT,
            preco REAL,
            nota REAL,
            link TEXT,
            canais TEXT,
            postado_em TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_postados_chave ON postados(chave, postado_em);
        CREATE TABLE IF NOT EXISTS estado (chave TEXT PRIMARY KEY, valor TEXT);
        """
    )
    # A primeira versão gravou horários 4h adiantados (ver _agora). Uma vez só,
    # traz de volta o que ficou "no futuro".
    if not con.execute("SELECT 1 FROM estado WHERE chave = 'fuso_corrigido'").fetchone():
        limite = _iso(_agora() + timedelta(minutes=30))
        con.execute("UPDATE precos SET visto_em = datetime(visto_em, '-4 hours') WHERE visto_em > ?", (limite,))
        con.execute("UPDATE postados SET postado_em = datetime(postado_em, '-4 hours') WHERE postado_em > ?", (limite,))
        con.execute("INSERT OR REPLACE INTO estado (chave, valor) VALUES ('fuso_corrigido', '1')")
        con.commit()
    return con


def estado_get(con, chave: str, padrao: str = "") -> str:
    linha = con.execute("SELECT valor FROM estado WHERE chave = ?", (chave,)).fetchone()
    return linha[0] if linha else padrao


def estado_set(con, chave: str, valor: str) -> None:
    con.execute(
        "INSERT INTO estado (chave, valor) VALUES (?, ?) "
        "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
        (chave, valor),
    )
    con.commit()


def _agora() -> datetime:
    """
    Hora de Brasília, sem depender do relógio "local" do Python.

    O iniciar.ps1 exporta TZ=America/Sao_Paulo para o Node, mas o Python do
    Windows lê TZ no formato antigo do C (tipo "EST5EDT"), entende aquele nome
    como UTC com horário de verão e adianta o relógio em 4 horas. Com isso o
    horário de silêncio e a contagem por hora sairiam deslocados. O instante
    UTC (time.time) não é afetado, então a conta parte dele.
    """
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Sao_Paulo")).replace(tzinfo=None)
    except Exception:
        # Sem a base tzdata no Windows: o Brasil não tem horário de verão desde 2019.
        return (datetime.now(timezone.utc) - timedelta(hours=3)).replace(tzinfo=None)


def _iso(momento: datetime) -> str:
    return momento.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Identidade do produto e link limpo
# ---------------------------------------------------------------------------
def _sem_fragmento(url: str) -> str:
    partes = urlparse(url)
    return urlunparse(partes._replace(fragment=""))


def _sem_query(url: str) -> str:
    partes = urlparse(url)
    return urlunparse(partes._replace(query="", fragment=""))


def identificar(link: str, plataforma: str = "") -> tuple[str, str] | None:
    """
    Devolve (chave_estavel, link_para_publicar) ou None quando o link não serve.

    A chave precisa ser a mesma a cada busca para o histórico e a regra de
    repetição funcionarem: os links das lojas carregam parâmetros de
    rastreamento que mudam a cada página carregada.
    """
    link = (link or "").strip()
    if not link.startswith("http"):
        return None
    baixo = link.lower()
    plat = (plataforma or "").lower()

    if "amazon." in baixo or "amazon" in plat:
        alvo = link
        if "/sspa/" in baixo:
            # Anúncio patrocinado: o produto vem codificado no parâmetro url.
            destino = parse_qs(urlparse(link).query).get("url", [""])[0]
            alvo = "https://www.amazon.com.br" + unquote(destino) if destino.startswith("/") else unquote(destino)
        achado = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", alvo, re.I)
        if not achado:
            return None
        asin = achado.group(1).upper()
        return f"amazon:{asin}", f"https://www.amazon.com.br/dp/{asin}"

    if "mercadoli" in baixo or "mercado livre" in plat:
        if "click1.mercadolivre" in baixo or "/mclics/" in baixo:
            return None  # anúncio patrocinado: redireciona e não dá para afiliar
        achado = re.search(r"(MLB)-?(\d{6,})", link, re.I)
        if not achado:
            return None
        return f"ml:MLB{achado.group(2)}", _sem_fragmento(link)

    if "kabum.com.br" in baixo:
        achado = re.search(r"/produto/(\d+)", link)
        if not achado:
            return None
        return f"kabum:{achado.group(1)}", _sem_query(link)

    if "aliexpress." in baixo:
        achado = re.search(r"/item/(\d+)", link)
        if not achado:
            return None
        return f"ali:{achado.group(1)}", f"https://pt.aliexpress.com/item/{achado.group(1)}.html"

    if "shopee" in baixo:
        achado = re.search(r"i\.(\d+)\.(\d+)", link) or re.search(r"/product/(\d+)/(\d+)", link)
        if not achado:
            return None
        return f"shopee:{achado.group(1)}.{achado.group(2)}", _sem_query(link)

    return f"url:{_sem_query(link)}", _sem_query(link)


# ---------------------------------------------------------------------------
# Relevância: o título tem a ver com o termo buscado?
# ---------------------------------------------------------------------------
def _normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", texto.lower()).strip()


def _tokens(texto: str) -> list[str]:
    return [t for t in _normalizar(texto).split() if t not in PALAVRAS_VAZIAS and len(t) >= 2]


def relevante(titulo: str, termo: str) -> bool:
    if not termo:
        return True
    tokens_titulo = set(_tokens(titulo))
    tokens_termo = _tokens(termo)
    if not tokens_termo:
        return True
    acertos = sum(1 for t in tokens_termo if t in tokens_titulo)
    if acertos < math.ceil(len(tokens_termo) / 2):
        return False
    # Acessório só passa se o próprio termo pediu acessório.
    primeiras = _tokens(titulo)[:4]
    for palavra in primeiras:
        if palavra in ACESSORIOS and palavra not in tokens_termo:
            return False
    return True


def assinatura_titulo(titulo: str) -> str:
    """Primeiras palavras normalizadas: pega o mesmo produto vindo de vendedores diferentes."""
    return " ".join(_tokens(titulo)[:6])


# ---------------------------------------------------------------------------
# Nota da oferta
# ---------------------------------------------------------------------------
def historico(con, chave: str, antes_de: datetime) -> list[float]:
    inicio = _iso(antes_de - timedelta(days=JANELA_HISTORICO_DIAS))
    linhas = con.execute(
        "SELECT preco FROM precos WHERE chave = ? AND visto_em >= ? AND visto_em < ?",
        (chave, inicio, _iso(antes_de)),
    ).fetchall()
    return [l[0] for l in linhas if l[0] and l[0] > 0]


def avaliar(prod: dict, hist: list[float], cfg: dict) -> dict | None:
    """
    Devolve {nota, desconto, motivo, destaque} ou None se a oferta não passa.

    Sem histórico suficiente vale o desconto que a loja anuncia. Com histórico,
    o desconto anunciado só é aceito se o preço estiver no menor patamar já
    visto; senão vale a queda real em relação à mediana observada.
    """
    preco = prod.get("preco")
    antes = prod.get("preco_original")
    if not isinstance(preco, (int, float)) or preco <= 0:
        return None
    if preco < _num(cfg, "PILOTO_PRECO_MIN"):
        return None
    if not prod.get("imagem_url") or not prod.get("titulo"):
        return None
    if str(prod.get("condicao") or "").lower().startswith("usad"):
        return None

    anunciado = (antes - preco) / antes if isinstance(antes, (int, float)) and antes > preco else 0.0
    minimo_exigido = _num(cfg, "PILOTO_DESCONTO_MIN") / 100

    real = None
    no_minimo = False
    if len(hist) >= MIN_OBSERVACOES:
        mediana = statistics.median(hist)
        real = (mediana - preco) / mediana if mediana > 0 else 0.0
        no_minimo = preco <= min(hist) * 1.01

    if real is None:
        efetivo, motivo = anunciado, "desconto anunciado"
    elif no_minimo:
        efetivo = max(anunciado, real)
        motivo = "menor preço já visto" + (" + desconto anunciado" if anunciado > real else "")
    else:
        efetivo, motivo = real, "comparado ao histórico"

    if efetivo < minimo_exigido or efetivo > DESCONTO_SUSPEITO:
        return None

    nota = efetivo * 100
    if real is None:
        loja = str(prod.get("plataforma") or "").lower().replace(" ", "")
        nota *= next((peso for nome, peso in CONFIANCA_ANUNCIADO.items() if nome in loja), 1.0)
    if no_minimo:
        nota += 5
    if prod.get("frete_gratis"):
        nota += 3
    if prod.get("cupom"):
        nota += 3
    if re.search(r"mais vendido|escolha da amazon|top vendas", str(prod.get("destaque") or ""), re.I):
        nota += 2

    pct = round(efetivo * 100)
    if anunciado >= efetivo - 0.005 and anunciado > 0:
        destaque = f"{pct}% OFF"
    else:
        destaque = f"PREÇO CAIU {pct}%"

    return {"nota": round(nota, 2), "desconto": efetivo, "motivo": motivo, "destaque": destaque}


# ---------------------------------------------------------------------------
# Seleção com limites
# ---------------------------------------------------------------------------
def pode_repostar(con, chave: str, preco: float, cfg: dict, agora: datetime) -> bool:
    janela = _iso(agora - timedelta(days=_num(cfg, "PILOTO_REPOSTAR_DIAS")))
    ultimo = con.execute(
        "SELECT preco FROM postados WHERE chave = ? AND postado_em >= ? ORDER BY postado_em DESC LIMIT 1",
        (chave, janela),
    ).fetchone()
    if not ultimo:
        return True
    queda = _num(cfg, "PILOTO_QUEDA_PARA_REPOSTAR") / 100
    return preco <= ultimo[0] * (1 - queda)


def postados_na_ultima_hora(con, agora: datetime) -> int:
    return con.execute(
        "SELECT COUNT(*) FROM postados WHERE postado_em >= ?", (_iso(agora - timedelta(hours=1)),)
    ).fetchone()[0]


def escolher(candidatos: list[dict], con, cfg: dict, agora: datetime) -> list[dict]:
    vagas_hora = int(_num(cfg, "PILOTO_MAX_POR_HORA")) - postados_na_ultima_hora(con, agora)
    vagas = min(int(_num(cfg, "PILOTO_POSTS_POR_CICLO")), vagas_hora)
    if vagas <= 0:
        return []

    por_loja_max = int(_num(cfg, "PILOTO_MAX_POR_LOJA_NO_CICLO", 1))
    escolhidos: list[dict] = []
    por_loja: dict[str, int] = {}
    termos_usados: set[str] = set()
    assinaturas: set[str] = set()

    ordenados = sorted(candidatos, key=lambda c: c["avaliacao"]["nota"], reverse=True)

    # Duas passadas: a primeira exige variedade de termo (um produto por busca),
    # a segunda preenche as vagas que sobrarem sem essa exigência.
    for exigir_termo_novo in (True, False):
        for cand in ordenados:
            if len(escolhidos) >= vagas:
                return escolhidos
            if cand in escolhidos:
                continue
            loja = cand["produto"].get("plataforma") or "?"
            if por_loja.get(loja, 0) >= por_loja_max:
                continue
            if exigir_termo_novo and cand["termo"] in termos_usados:
                continue
            assinatura = assinatura_titulo(cand["produto"].get("titulo", ""))
            if assinatura in assinaturas:
                continue
            if not pode_repostar(con, cand["chave"], cand["produto"]["preco"], cfg, agora):
                continue
            escolhidos.append(cand)
            por_loja[loja] = por_loja.get(loja, 0) + 1
            termos_usados.add(cand["termo"])
            assinaturas.add(assinatura)
    return escolhidos


def em_silencio(cfg: dict, agora: datetime) -> bool:
    faixa = str(cfg.get("PILOTO_SILENCIO") or "").strip()
    achado = re.fullmatch(r"(\d{1,2})\s*-\s*(\d{1,2})", faixa)
    if not achado:
        return False
    inicio, fim = int(achado.group(1)) % 24, int(achado.group(2)) % 24
    hora = agora.hour
    if inicio == fim:
        return False
    if inicio < fim:
        return inicio <= hora < fim
    return hora >= inicio or hora < fim


# ---------------------------------------------------------------------------
# Busca e publicação
# ---------------------------------------------------------------------------
def categorias_do_rodizio(cfg: dict) -> list[str]:
    import categories

    todas = [c["chave"] for c in sorted(categories.listar_categorias(), key=lambda c: c["prioridade"])]
    pedido = str(cfg.get("PILOTO_CATEGORIAS") or "").strip().lower()
    if not pedido or pedido in ("todas", "all", "tudo"):
        return todas
    escolhidas = [c.strip() for c in pedido.split(",") if c.strip() in todas]
    return escolhidas or todas


def proximas_categorias(con, cfg: dict) -> list[str]:
    lista = categorias_do_rodizio(cfg)
    quantas = max(1, int(_num(cfg, "PILOTO_CATEGORIAS_POR_CICLO", 1)))
    try:
        posicao = int(estado_get(con, "rodizio", "0"))
    except ValueError:
        posicao = 0
    escolhidas = [lista[(posicao + i) % len(lista)] for i in range(min(quantas, len(lista)))]
    estado_set(con, "rodizio", str((posicao + len(escolhidas)) % len(lista)))
    return escolhidas


def buscar_categoria(chave_categoria: str, cfg: dict, trava: threading.Lock | None) -> list[tuple[str, dict]]:
    """Devolve [(termo, produto_bruto)] de todas as lojas para todos os termos."""
    import categories
    import marketplaces

    info = categories.CATEGORIAS[chave_categoria]
    lojas = marketplaces.resolver_plataformas(cfg.get("PILOTO_LOJAS") or "all")
    itens_por_loja = int(_num(cfg, "PILOTO_ITENS_POR_LOJA", 1))
    saida: list[tuple[str, dict]] = []

    for termo in info.get("termos", []):
        for loja in lojas:
            _fase("buscando", f"{info['emoji']} {info['nome']} › '{termo}' em {marketplaces.rotulo(loja)}")
            # Espera a busca manual do portal terminar, se houver uma.
            if trava is not None and not trava.acquire(timeout=900):
                log(f"Pulei '{termo}' em {loja}: a busca manual do portal não liberou.")
                continue
            try:
                scraper = marketplaces.criar_scraper(loja, headless=True)
                itens = scraper.search_products(query=termo, max_pages=1, max_items=itens_por_loja) or []
                if not itens and scraper.last_error:
                    log(f"{marketplaces.rotulo(loja)} sem resultado para '{termo}': {scraper.last_error[:160]}")
            except Exception as exc:
                log(f"Erro buscando '{termo}' em {loja}: {exc}")
                itens = []
            finally:
                if trava is not None:
                    trava.release()
            saida.extend((termo, item) for item in itens)
    return saida


def bot_url() -> str:
    return (os.getenv("BOT_API_URL") or "http://127.0.0.1:3005").rstrip("/")


def bot_headers() -> dict:
    token = os.getenv("BOT_API_TOKEN", "")
    return {"X-Api-Token": token} if token else {}


def publicar(produto: dict) -> tuple[bool, str]:
    try:
        resp = requests.post(f"{bot_url()}/api/post-deal", json=produto, timeout=90, headers=bot_headers())
        dados = resp.json()
        if dados.get("success"):
            return True, ", ".join(dados.get("channels") or [])
        return False, dados.get("message") or dados.get("error") or f"HTTP {resp.status_code}"
    except Exception as exc:
        return False, str(exc)


def processar(brutos: list[tuple[str, dict]], categoria_nome: str, con, cfg: dict, agora: datetime) -> list[dict]:
    """Grava histórico e devolve os candidatos aprovados (sem publicar)."""
    candidatos: list[dict] = []
    vistos: set[str] = set()
    for termo, item in brutos:
        ident = identificar(item.get("link", ""), item.get("plataforma", ""))
        if not ident:
            continue
        chave, link = ident
        if chave in vistos:
            continue
        vistos.add(chave)

        preco = item.get("preco")
        hist = historico(con, chave, agora)
        if isinstance(preco, (int, float)) and preco > 0:
            con.execute("INSERT INTO precos (chave, preco, visto_em) VALUES (?, ?, ?)", (chave, float(preco), _iso(agora)))

        if not relevante(item.get("titulo", ""), termo):
            continue
        avaliacao = avaliar(item, hist, cfg)
        if not avaliacao:
            continue

        produto = dict(item)
        produto["link"] = link
        produto["categoria"] = categoria_nome
        candidatos.append({"chave": chave, "termo": termo, "produto": produto, "avaliacao": avaliacao})
    con.commit()
    return candidatos


def montar_postagem(cand: dict) -> dict:
    p = cand["produto"]
    original = p.get("preco_original")
    if not (isinstance(original, (int, float)) and original > p["preco"]):
        original = None
    return {
        "titulo": p.get("titulo"),
        "preco": p.get("preco"),
        "preco_original": original,
        "cupom": p.get("cupom"),
        "destaque": cand["avaliacao"]["destaque"],
        "link": p.get("link"),
        "imagem_url": p.get("imagem_url"),
        "plataforma": p.get("plataforma"),
    }


def rodar_ciclo(trava: threading.Lock | None = None, con=None) -> dict:
    cfg = ler_config()
    con = con or conectar()
    inicio = _agora()
    silencio = em_silencio(cfg, inicio)
    resumo = {"categorias": [], "vistos": 0, "candidatos": 0, "publicados": 0, "silencio": silencio}

    for chave_cat in proximas_categorias(con, cfg):
        import categories

        info = categories.CATEGORIAS[chave_cat]
        with _trava_estado:
            _estado["ultima_categoria"] = f"{info['emoji']} {info['nome']}"
        log(f"Ciclo: {info['emoji']} {info['nome']}" + (" (horário de silêncio: só coleta preços)" if silencio else ""))

        brutos = buscar_categoria(chave_cat, cfg, trava)
        agora = _agora()
        candidatos = processar(brutos, info["nome"], con, cfg, agora)
        resumo["categorias"].append(chave_cat)
        resumo["vistos"] += len(brutos)
        resumo["candidatos"] += len(candidatos)
        log(f"{len(brutos)} produtos vistos, {len(candidatos)} passaram nas regras.")

        if silencio or not candidatos:
            continue
        if estado_get(con, "pausado", "0") == "1":
            log("Publicação pausada pelo painel: nada foi enviado.")
            continue

        escolhidos = escolher(candidatos, con, cfg, agora)
        if not escolhidos:
            log("Nenhuma oferta nova: as boas já foram postadas ou o limite por hora foi atingido.")
            continue

        gap_min = float(os.getenv("PUBLISH_MIN_DELAY", "6"))
        gap_max = float(os.getenv("PUBLISH_MAX_DELAY", "14"))
        for indice, cand in enumerate(escolhidos):
            p = cand["produto"]
            _fase("publicando", p.get("titulo", "")[:80])
            ok, detalhe = publicar(montar_postagem(cand))
            if ok:
                con.execute(
                    "INSERT INTO postados (chave, plataforma, titulo, preco, nota, link, canais, postado_em) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (cand["chave"], p.get("plataforma"), p.get("titulo"), p.get("preco"),
                     cand["avaliacao"]["nota"], p.get("link"), detalhe, _iso(_agora())),
                )
                con.commit()
                resumo["publicados"] += 1
                log(f"✅ Postado em {detalhe}: [{p.get('plataforma')}] {p.get('titulo', '')[:70]} "
                    f"R$ {p['preco']:.2f} ({cand['avaliacao']['destaque']}, nota {cand['avaliacao']['nota']})")
            else:
                log(f"❌ Falhou: {p.get('titulo', '')[:60]} — {detalhe}")
            if indice < len(escolhidos) - 1:
                time.sleep(random.uniform(gap_min, gap_max))

    return resumo


# ---------------------------------------------------------------------------
# Laço em segundo plano
# ---------------------------------------------------------------------------
def _laco(trava: threading.Lock | None) -> None:
    con = conectar()
    # Ao abrir o programa o primeiro ciclo segue o intervalo normal (para
    # testar na hora, use "Rodar agora" no portal). O piso de 45s dá tempo do
    # bot e do WhatsApp terminarem de conectar.
    cfg = ler_config()
    espera = max(45.0, 60 * random.uniform(_num(cfg, "PILOTO_INTERVALO_MIN", 1),
                                           max(_num(cfg, "PILOTO_INTERVALO_MIN", 1), _num(cfg, "PILOTO_INTERVALO_MAX", 1))))
    proximo = _agora() + timedelta(seconds=espera)
    with _trava_estado:
        _estado["proximo_ciclo"] = _iso(proximo)
    _fase("aguardando", f"primeiro ciclo às {proximo:%H:%M}")
    log(f"Primeiro ciclo às {proximo:%H:%M}.")
    _acordar.wait(espera)
    _acordar.clear()
    while True:
        cfg = ler_config()
        if str(cfg.get("PILOTO_ATIVO")).strip() == "0":
            _fase("desligado", "PILOTO_ATIVO=0 no config.txt")
            _acordar.wait(300)
            _acordar.clear()
            continue

        comeco = time.time()
        with _trava_estado:
            _estado["ciclo"] += 1
            _estado["proximo_ciclo"] = None
        try:
            resumo = rodar_ciclo(trava, con)
            with _trava_estado:
                _estado["ultimo_erro"] = None
            log(f"Fim do ciclo: {resumo['publicados']} publicada(s) de {resumo['candidatos']} candidata(s).")
        except Exception as exc:
            with _trava_estado:
                _estado["ultimo_erro"] = f"{_agora():%d/%m %H:%M} {exc}"
            log(f"Erro no ciclo: {exc}\n{traceback.format_exc()}")
        with _trava_estado:
            _estado["ultimo_ciclo"] = _iso(_agora())

        cfg = ler_config()
        minutos = random.uniform(_num(cfg, "PILOTO_INTERVALO_MIN", 1), max(_num(cfg, "PILOTO_INTERVALO_MIN", 1), _num(cfg, "PILOTO_INTERVALO_MAX", 1)))
        if em_silencio(cfg, _agora()):
            minutos *= 2  # de madrugada só coleta preço, sem pressa
        espera = max(60.0, minutos * 60 - (time.time() - comeco))
        proximo = _agora() + timedelta(seconds=espera)
        with _trava_estado:
            _estado["proximo_ciclo"] = _iso(proximo)
        _fase("aguardando", f"próximo ciclo às {proximo:%H:%M}")
        _acordar.wait(espera)
        _acordar.clear()


def iniciar_em_segundo_plano(trava: threading.Lock | None = None) -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    with _trava_estado:
        _estado["rodando"] = True
        _estado["fase"] = "iniciando"
        _estado["detalhe"] = ""
    _thread = threading.Thread(target=_laco, args=(trava,), name="piloto-automatico", daemon=True)
    _thread.start()
    log("Piloto automático iniciado.")


def rodar_agora() -> None:
    _acordar.set()


def pausar(pausado: bool) -> None:
    con = conectar()
    estado_set(con, "pausado", "1" if pausado else "0")
    log("Publicação pausada pelo painel." if pausado else "Publicação retomada pelo painel.")


def status() -> dict:
    cfg = ler_config()
    con = conectar()
    agora = _agora()
    ultimos = [
        {"plataforma": l[0], "titulo": l[1], "preco": l[2], "nota": l[3], "link": l[4], "canais": l[5], "postado_em": l[6]}
        for l in con.execute(
            "SELECT plataforma, titulo, preco, nota, link, canais, postado_em FROM postados ORDER BY postado_em DESC LIMIT 15"
        ).fetchall()
    ]
    hoje = con.execute("SELECT COUNT(*) FROM postados WHERE postado_em >= ?", (agora.strftime("%Y-%m-%d 00:00:00"),)).fetchone()[0]
    produtos = con.execute("SELECT COUNT(DISTINCT chave) FROM precos").fetchone()[0]
    with _trava_estado:
        info = dict(_estado)
    info.update({
        "ativo_no_config": str(cfg.get("PILOTO_ATIVO")).strip() != "0",
        "pausado": estado_get(con, "pausado", "0") == "1",
        "silencio_agora": em_silencio(cfg, agora),
        "postados_hoje": hoje,
        "postados_ultima_hora": postados_na_ultima_hora(con, agora),
        "produtos_monitorados": produtos,
        "ultimos": ultimos,
        "regras": {k: cfg[k] for k in PADROES},
    })
    return info


if __name__ == "__main__":
    # Execução manual de um ciclo só, para teste: python piloto_automatico.py
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(BASE_DIR, ".env.local"))
    except ImportError:
        pass
    print(json.dumps(rodar_ciclo(), ensure_ascii=False, indent=2))
