"""
Piloto automático: busca, escolhe e publica ofertas sozinho.

Roda dentro do processo do portal (servir.py liga uma thread em segundo plano),
então sobe com o INICIAR.bat e morre com o PARAR.bat sem processo extra. Usa o
mesmo SCRAPE_LOCK das buscas manuais: nunca abre dois Firefox ao mesmo tempo.

Um ciclo:
  1. pega a próxima categoria do rodízio (ordem de prioridade do categories.py);
  2. busca cada termo dela em cada loja de PILOTO_LOJAS;
  3. grava o preço de tudo que viu no histórico (data/piloto.db);
  4. dá nota a cada produto e escolhe os melhores, respeitando os limites e a
     ordem de prioridade das lojas (PILOTO_PRIORIDADE_LOJAS);
  5. publica pelo bot (/api/post-deal), que aplica o link de afiliado da loja
     e manda a mesma oferta para o catálogo do site (espelho dos grupos);
  6. avisa o site do novo preço dos produtos já postados que viu de novo;
     (antes de tudo, o caçador de cupons — cupons.py — relê os cupons oficiais
     e confere em quais ofertas cada um vale; o cupom entra no preço final e
     o código vai na postagem);
  7. dorme um intervalo aleatório entre PILOTO_INTERVALO_MIN e _MAX minutos.

Por que guardar histórico: "de R$ 999 por R$ 499" é muitas vezes um preço
"de" inflado. Depois de ver o mesmo produto em alguns dias diferentes, o piloto
compara o preço de hoje com o preço normal que ele mesmo observou e só confia
no desconto anunciado quando o preço está de fato no menor patamar já visto.
O aprendizado (aprendizado.py) resume o histórico em um preço por dia, pega o
"sobe antes, dá desconto depois" e aprende o quanto o "de/por" de cada loja
costuma mentir (PILOTO_APRENDIZADO=0 volta à regra antiga).

Preço de mercado: produto novo ainda não tem histórico, e é aí que entra o
anúncio de "40% OFF" num PS5 de R$ 7.400 (o preço "de" inventado pelo
vendedor). Cada busca traz o mesmo produto de várias lojas; o piloto guarda o
preço de tudo que é relevante por termo e compara a oferta com o que as OUTRAS
lojas cobram (mediana e quartis dos últimos dias). Oferta muito acima do
mercado é descartada; preço "de" acima do teto do mercado é tratado como
inventado (o desconto passa a ser o real, contra o mercado, e o "De:" some da
postagem); preço muito abaixo de tudo é acessório ou anúncio falso.

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

import aprendizado
import cupons

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.abspath(os.path.join(BASE_DIR, "..", ".."))
CONFIG_TXT = os.path.join(RAIZ, "config.txt")
DB_PATH = os.path.join(BASE_DIR, "data", "piloto.db")
LOG_PATH = os.path.join(RAIZ, "logs", "piloto.log")

PADROES = {
    "PILOTO_ATIVO": "1",
    "PILOTO_INTERVALO_MIN": "10",
    "PILOTO_INTERVALO_MAX": "20",
    "PILOTO_LOJAS": "mercadolivre,amazon,kabum,aliexpress",
    "PILOTO_PRIORIDADE_LOJAS": "mercadolivre,amazon,kabum,aliexpress",
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
    "PILOTO_ATUALIZAR_SITE": "1",
    "PILOTO_TETO_MERCADO": "1.3",
    "PILOTO_BLOQUEAR_USADOS": "1",
    "PILOTO_CUPONS": "1",
    "PILOTO_CUPONS_HORAS": "2",
    "PILOTO_CUPOM_POSTS_POR_CICLO": "1",
    "PILOTO_CUPOM_PAGINAS": "10",
    "PILOTO_CUPONS_ML": "1",
    "PILOTO_CUPONS_ML_CATEGORIAS": cupons.ML_CATEGORIAS_PADRAO,
    "PILOTO_AVISOS_CUPOM_POR_DIA": "6",
    "PILOTO_AVISO_CUPOM_INTERVALO": "90",
    "PILOTO_APRENDIZADO": "1",
}

# Desconto acima disso quase sempre é erro de cadastro ou preço "de" fictício.
DESCONTO_SUSPEITO = 0.85

# Lojas cujo "de/por" costuma ser inflado de forma permanente. Enquanto não há
# histórico, o desconto anunciado delas pesa menos na nota (não no texto).
CONFIANCA_ANUNCIADO = {"aliexpress": 0.6}

# Observações mínimas para o histórico passar a valer mais que o "de/por".
MIN_OBSERVACOES = 3
JANELA_HISTORICO_DIAS = 30

# Preço de mercado: produtos diferentes (e de pelo menos 2 lojas) que a mesma
# busca precisa ter mostrado para a comparação valer, e a janela considerada.
MIN_MERCADO = 6
MIN_MERCADO_OUTRAS_LOJAS = 4
JANELA_MERCADO_DIAS = 14
# Abaixo desta fração da mediana (e fora da curva) é peça, acessório ou golpe.
PISO_MERCADO = 0.35

# Uma busca (uma loja, um termo) leva de 2 a 60 s. Passou disto, o navegador
# travou: o piloto desiste dela e segue o ciclo, em vez de ficar parado horas.
TEMPO_MAX_BUSCA_S = 240
# Loja que travou fica de fora por este tempo (o Firefox dela pode seguir preso).
PAUSA_LOJA_TRAVADA_MIN = 30
_lojas_travadas: dict[str, float] = {}

# Usado, recondicionado, vitrine: o grupo divulga produto novo.
USADO_RE = re.compile(
    r"\b(usad[oa]s?|semi ?nov[oa]s?|recondicionad[oa]s?|refurbished|renewed|vitrine|open ?box|"
    r"reembalad[oa]s?|mostruario|com defeito|para pecas|retirada de pecas)\b"
)

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
    """
    config.txt tem prioridade sobre o padrão; config.local.txt (fora do git)
    sobre o config.txt; variável de ambiente sobre todos.
    """
    cfg = dict(PADROES)
    for caminho in (CONFIG_TXT, os.path.join(RAIZ, "config.local.txt")):
        try:
            with open(caminho, encoding="utf-8-sig") as arquivo:
                for linha in arquivo:
                    texto = linha.strip()
                    if not texto or texto.startswith("#") or "=" not in texto:
                        continue
                    chave, valor = texto.split("=", 1)
                    chave = chave.strip()
                    if chave in cfg and valor.strip():
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
    # Termo, loja e "serve de referência": base do preço de mercado por busca.
    colunas = {linha[1] for linha in con.execute("PRAGMA table_info(precos)")}
    for coluna, tipo in (("termo", "TEXT"), ("loja", "TEXT"), ("ref", "INTEGER")):
        if coluna not in colunas:
            con.execute(f"ALTER TABLE precos ADD COLUMN {coluna} {tipo}")
    con.execute("CREATE INDEX IF NOT EXISTS idx_precos_termo ON precos(termo, visto_em)")
    # A primeira versão gravou horários 4h adiantados (ver _agora). Uma vez só,
    # traz de volta o que ficou "no futuro".
    if not con.execute("SELECT 1 FROM estado WHERE chave = 'fuso_corrigido'").fetchone():
        limite = _iso(_agora() + timedelta(minutes=30))
        con.execute("UPDATE precos SET visto_em = datetime(visto_em, '-4 hours') WHERE visto_em > ?", (limite,))
        con.execute("UPDATE postados SET postado_em = datetime(postado_em, '-4 hours') WHERE postado_em > ?", (limite,))
        con.execute("INSERT OR REPLACE INTO estado (chave, valor) VALUES ('fuso_corrigido', '1')")
        con.commit()
    cupons.preparar_tabelas(con)
    aprendizado.preparar_tabelas(con)
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
    if not termo or termo.startswith("cupom "):
        return True  # produto da lista oficial do cupom: a loja já disse que vale
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


def usado(prod: dict) -> bool:
    """Condição do anúncio ou título dizendo usado, seminovo, recondicionado, vitrine..."""
    if str(prod.get("condicao") or "").lower().startswith("usad"):
        return True
    return bool(USADO_RE.search(_normalizar(prod.get("titulo") or "")))


def _loja(prod: dict) -> str:
    return str(prod.get("plataforma") or "").strip().lower()


def mercado_para(precos_do_termo: dict[str, tuple[float, str]], chave: str, loja: str) -> dict | None:
    """
    Referência de preço para uma oferta: o que as OUTRAS lojas cobram na mesma
    busca (se houver o bastante); senão, todas as lojas juntas. None quando a
    amostra é pequena demais para dizer alguma coisa.
    """
    outros = [(p, l) for c, (p, l) in precos_do_termo.items() if c != chave]
    de_fora = [p for p, l in outros if l != loja]
    if len(de_fora) >= MIN_MERCADO_OUTRAS_LOJAS:
        amostra = de_fora
    elif len(outros) >= MIN_MERCADO and len({l for _, l in outros}) >= 2:
        amostra = [p for p, _ in outros]
    else:
        return None
    if len(amostra) < 4:
        return None
    q1, mediana, q3 = statistics.quantiles(sorted(amostra), n=4)
    return {"mediana": mediana, "q1": q1, "q3": q3, "n": len(amostra)}


def avaliar(prod: dict, hist: list[float], cfg: dict, cupom: dict | None = None,
            mercado: dict | None = None, descartes: dict | None = None,
            ref: dict | None = None, confianca_loja: tuple[float, bool] | None = None) -> dict | None:
    """
    Devolve {nota, desconto, motivo, destaque} ou None se a oferta não passa.

    Sem histórico suficiente vale o desconto que a loja anuncia. Com histórico,
    o desconto anunciado só é aceito se o preço estiver no menor patamar já
    visto; senão vale a queda real em relação à mediana observada.

    Com cupom válido, a conta usa o preço final com o cupom aplicado (o
    histórico guarda o preço sem cupom, então o cupom aparece como queda real).

    Com `mercado` (ver mercado_para), a oferta também precisa caber no preço
    que as outras lojas praticam. `descartes` conta os motivos para o log.

    Com `ref` (aprendizado.referencia: produto com alguns dias de histórico),
    o preço normal e o menor preço vêm do resumo por dia, já descontada a
    alta suspeita antes da promoção. `confianca_loja` = (peso, aprendido): o
    quanto o "de/por" da loja costuma ser verdade; aprendido, ele também
    decide se a oferta sem histórico passa.
    """
    def descartar(motivo: str) -> None:
        if descartes is not None:
            descartes[motivo] = descartes.get(motivo, 0) + 1

    preco = prod.get("preco")
    antes = prod.get("preco_original")
    if not isinstance(preco, (int, float)) or preco <= 0:
        return None
    final_cupom = (cupom or {}).get("preco_com_cupom")
    if isinstance(final_cupom, (int, float)) and 0 < final_cupom < preco:
        if not (isinstance(antes, (int, float)) and antes > preco):
            antes = preco
        preco = final_cupom
    else:
        final_cupom = None
    if preco < _num(cfg, "PILOTO_PRECO_MIN"):
        return None
    if not prod.get("imagem_url") or not prod.get("titulo"):
        return None
    if str(cfg.get("PILOTO_BLOQUEAR_USADOS")).strip() != "0" and usado(prod):
        descartar("usado ou recondicionado")
        return None

    anunciado = (antes - preco) / antes if isinstance(antes, (int, float)) and antes > preco else 0.0
    minimo_exigido = _num(cfg, "PILOTO_DESCONTO_MIN") / 100

    # Preço de mercado: o que as outras lojas cobram na mesma busca.
    de_confiavel = True
    contra_mercado = None
    if mercado:
        m, q1, q3 = mercado["mediana"], mercado["q1"], mercado["q3"]
        iqr = max(0.0, q3 - q1)
        teto = max(m * _num(cfg, "PILOTO_TETO_MERCADO", 1.0), q3 + 1.5 * iqr)
        if preco > teto:
            descartar("acima do preço de mercado")
            return None
        if preco < min(m * PISO_MERCADO, q1 - 1.5 * iqr):
            descartar("barato demais para a busca (acessório ou anúncio falso)")
            return None
        contra_mercado = (m - preco) / m if m > 0 else 0.0
        # Preço "de" acima do teto do mercado: o desconto anunciado é inventado.
        if isinstance(antes, (int, float)) and antes > teto:
            de_confiavel = False
            anunciado = max(0.0, contra_mercado)

    real = None
    no_minimo = False
    maquiado = False
    if ref and ref.get("normal", 0) > 0:
        real = (ref["normal"] - preco) / ref["normal"]
        # Menor preço só conta se for de fato abaixo do normal: produto que
        # nunca muda de preço está sempre "no mínimo" e isso não é promoção.
        no_minimo = preco <= ref["minimo"] * 1.01 and real >= 0.03
        maquiado = bool(ref.get("maquiado"))
    elif len(hist) >= MIN_OBSERVACOES:
        mediana = statistics.median(hist)
        real = (mediana - preco) / mediana if mediana > 0 else 0.0
        no_minimo = preco <= min(hist) * 1.01

    de_exibir = None
    if real is None:
        efetivo, motivo = anunciado, ("desconto anunciado" if de_confiavel else "abaixo do preço de mercado")
    elif ref:
        # Com dias de histórico vale só a queda real contra o preço normal: o
        # "de" da loja não entra na conta (é ele que costuma ser inflado).
        efetivo = real
        motivo = (f"menor preço em {ref['dias']} dias" if no_minimo
                  else f"abaixo do preço normal de {ref['dias']} dias")
        # Na postagem, o "De:" passa a ser o preço normal observado quando o
        # da loja está muito acima dele (ou não existe).
        if not (isinstance(antes, (int, float)) and antes <= ref["normal"] * 1.15):
            de_exibir = round(ref["normal"], 2)
    elif no_minimo:
        efetivo = max(anunciado, real)
        motivo = "menor preço já visto" + (" + desconto anunciado" if anunciado > real else "")
    else:
        efetivo, motivo = real, "comparado ao histórico"
    if maquiado:
        motivo += "; preço normal de antes da alta recente"

    # Sem histórico próprio, o peso do "de/por" da loja (aprendido ou fixo).
    peso, peso_aprendido = 1.0, False
    if real is None:
        if confianca_loja:
            peso, peso_aprendido = confianca_loja
        else:
            loja = str(prod.get("plataforma") or "").lower().replace(" ", "")
            peso = next((p for nome, p in CONFIANCA_ANUNCIADO.items() if nome in loja), 1.0)
    # Desconto estimado de verdade: o anunciado vezes o quanto a loja costuma cumprir.
    estimado = efetivo * peso if (real is None and peso_aprendido and de_confiavel) else efetivo

    if estimado < minimo_exigido or efetivo > DESCONTO_SUSPEITO:
        if not de_confiavel:
            descartar('preço "de" inventado')
        elif maquiado:
            descartar("preço subiu antes do desconto")
        elif estimado < efetivo and efetivo >= minimo_exigido:
            descartar('"de/por" da loja costuma ser inflado')
        return None

    nota = efetivo * 100
    if real is None:
        nota *= peso
    if no_minimo:
        nota += 5
    if prod.get("frete_gratis"):
        nota += 3
    if final_cupom:
        nota += 4
    elif cupom or prod.get("cupom"):
        nota += 2
    if re.search(r"mais vendido|escolha da amazon|top vendas", str(prod.get("destaque") or ""), re.I):
        nota += 2

    pct = round(efetivo * 100)
    if final_cupom:
        destaque = f"{pct}% OFF COM CUPOM"
    elif not de_confiavel:
        destaque = f"{pct}% ABAIXO DO MERCADO"
    elif de_exibir or (anunciado >= efetivo - 0.005 and anunciado > 0):
        destaque = f"{pct}% OFF"
    else:
        destaque = f"PREÇO CAIU {pct}%"

    return {"nota": round(nota, 2), "desconto": efetivo, "motivo": motivo, "destaque": destaque,
            "preco_final": round(preco, 2), "de_confiavel": de_confiavel, "maquiado": maquiado,
            "de_exibir": de_exibir,
            "mercado": round(mercado["mediana"], 2) if mercado else None}


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


def prioridade_das_lojas(cfg: dict):
    """
    Posição da loja na ordem de PILOTO_PRIORIDADE_LOJAS (0 = primeira). Loja
    fora da lista vem depois de todas. Compara pelo nome sem espaço nem acento:
    "Amazon Brasil" casa com "amazon", "KaBuM!" com "kabum".
    """
    ordem = [_normalizar(p).replace(" ", "") for p in str(cfg.get("PILOTO_PRIORIDADE_LOJAS") or "").split(",")]
    ordem = [p for p in ordem if p]

    def posicao(plataforma: str) -> int:
        nome = _normalizar(plataforma).replace(" ", "")
        return next((i for i, chave in enumerate(ordem) if chave in nome), len(ordem))

    return posicao


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
    max_de_cupom = int(_num(cfg, "PILOTO_CUPOM_POSTS_POR_CICLO"))
    de_cupom = 0

    # Primeiro a loja (ordem de PILOTO_PRIORIDADE_LOJAS), depois a nota: uma
    # oferta do Mercado Livre que passou em todas as regras sai antes de uma
    # do AliExpress, mesmo que esta tenha desconto maior.
    posicao = prioridade_das_lojas(cfg)
    ordenados = sorted(
        candidatos, key=lambda c: (posicao(c["produto"].get("plataforma") or ""), -c["avaliacao"]["nota"])
    )

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
            veio_de_cupom = cand["termo"].startswith("cupom ")
            if veio_de_cupom and de_cupom >= max_de_cupom:
                continue
            assinatura = assinatura_titulo(cand["produto"].get("titulo", ""))
            if assinatura in assinaturas:
                continue
            preco_ref = cand["avaliacao"].get("preco_final") or cand["produto"]["preco"]
            if not pode_repostar(con, cand["chave"], preco_ref, cfg, agora):
                continue
            escolhidos.append(cand)
            de_cupom += int(veio_de_cupom)
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


def _buscar_com_limite(loja: str, termo: str, itens: int, segundos: float) -> tuple[list, str | None]:
    """
    Roda a busca numa thread à parte e espera no máximo `segundos`. Se o
    navegador travar (aconteceu em 12/10: a Amazon ficou 1h45 presa ao abrir o
    Firefox), levanta TimeoutError e o ciclo continua com as outras lojas.
    """
    import marketplaces

    resultado: dict = {}

    def alvo():
        try:
            scraper = marketplaces.criar_scraper(loja, headless=True)
            resultado["itens"] = scraper.search_products(query=termo, max_pages=1, max_items=itens) or []
            resultado["erro"] = scraper.last_error
        except Exception as exc:  # repassado para quem chamou
            resultado["exc"] = exc

    t = threading.Thread(target=alvo, name=f"busca-{loja}", daemon=True)
    t.start()
    t.join(segundos)
    if t.is_alive():
        raise TimeoutError(f"a busca travou (mais de {int(segundos)} s sem responder)")
    if "exc" in resultado:
        raise resultado["exc"]
    return resultado.get("itens", []), resultado.get("erro")


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
            pausa_ate = _lojas_travadas.get(loja, 0)
            if pausa_ate > time.time():
                continue
            _fase("buscando", f"{info['emoji']} {info['nome']} › '{termo}' em {marketplaces.rotulo(loja)}")
            # Espera a busca manual do portal terminar, se houver uma.
            if trava is not None and not trava.acquire(timeout=900):
                log(f"Pulei '{termo}' em {loja}: a busca manual do portal não liberou.")
                continue
            try:
                itens, erro = _buscar_com_limite(loja, termo, itens_por_loja, TEMPO_MAX_BUSCA_S)
                if not itens and erro:
                    log(f"{marketplaces.rotulo(loja)} sem resultado para '{termo}': {erro[:160]}")
            except TimeoutError as exc:
                _lojas_travadas[loja] = time.time() + PAUSA_LOJA_TRAVADA_MIN * 60
                log(f"⚠️ {marketplaces.rotulo(loja)}: {exc} em '{termo}'. Pulei a loja pelos próximos "
                    f"{PAUSA_LOJA_TRAVADA_MIN} min e segui o ciclo.")
                itens = []
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


def processar(brutos: list[tuple[str, dict]], categoria_nome: str, con, cfg: dict, agora: datetime,
              registrar_precos: bool = True) -> list[dict]:
    """Grava histórico e devolve os candidatos aprovados (sem publicar)."""
    candidatos: list[dict] = []
    vistos: set[str] = set()
    descartes: dict[str, int] = {}
    mercados = precos_de_mercado(brutos, con, agora)
    usar_aprendizado = str(cfg.get("PILOTO_APRENDIZADO")).strip() != "0"
    aprendido = aprendizado.carregar(con) if usar_aprendizado else {}
    for termo, item in brutos:
        ident = identificar(item.get("link", ""), item.get("plataforma", ""))
        if not ident:
            continue
        chave, link = ident
        if chave in vistos:
            continue
        vistos.add(chave)

        preco = item.get("preco")
        if usar_aprendizado:
            ref, hist = aprendizado.referencia_do_produto(con, chave, agora)
        else:
            ref, hist = None, historico(con, chave, agora)
        eh_relevante = relevante(item.get("titulo", ""), termo)
        if registrar_precos and isinstance(preco, (int, float)) and preco > 0:
            de = item.get("preco_original")
            de = float(de) if isinstance(de, (int, float)) and de > preco else None
            con.execute(
                "INSERT INTO precos (chave, preco, visto_em, termo, loja, ref, de) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chave, float(preco), _iso(agora), termo, _loja(item), int(eh_relevante and not usado(item)), de),
            )
            aprendizado.registrar_produto(con, chave, item, link, termo, categoria_nome, agora)

        if not eh_relevante:
            continue
        cupom = None
        if str(cfg.get("PILOTO_CUPONS")).strip() != "0":
            try:
                cupom = cupons.melhor_cupom(con, chave, item, agora)
            except Exception as exc:
                log(f"Cupom: falha ao conferir {chave} ({exc})")
        mercado = None
        if termo in mercados:
            mercado = mercado_para(mercados[termo], chave, _loja(item))
        avaliacao = avaliar(item, hist, cfg, cupom, mercado, descartes,
                            ref=ref, confianca_loja=aprendizado.confianca(aprendido, chave) if usar_aprendizado else None)
        if not avaliacao:
            continue

        produto = dict(item)
        produto["link"] = link
        produto["categoria"] = categoria_nome
        produto["cupom_info"] = cupom
        candidatos.append({"chave": chave, "termo": termo, "produto": produto, "avaliacao": avaliacao})
    con.commit()
    if descartes:
        log("Descartadas: " + ", ".join(f"{n} {motivo}" for motivo, n in sorted(descartes.items(), key=lambda x: -x[1])) + ".")
    return candidatos


def precos_de_mercado(brutos: list[tuple[str, dict]], con, agora: datetime) -> dict[str, dict[str, tuple[float, str]]]:
    """
    Por termo de busca: {chave: (preço, loja)} de tudo que é relevante e novo,
    juntando esta busca com os últimos JANELA_MERCADO_DIAS dias (o preço mais
    recente de cada produto). As listas de cupom ("cupom X") misturam produtos
    de todo tipo e ficam de fora.
    """
    atuais: dict[str, dict[str, tuple[float, str]]] = {}
    for termo, item in brutos:
        if termo.startswith("cupom "):
            continue
        preco = item.get("preco")
        if not isinstance(preco, (int, float)) or preco <= 0:
            continue
        if not relevante(item.get("titulo", ""), termo) or usado(item):
            continue
        ident = identificar(item.get("link", ""), item.get("plataforma", ""))
        if ident:
            atuais.setdefault(termo, {})[ident[0]] = (float(preco), _loja(item))

    desde = _iso(agora - timedelta(days=JANELA_MERCADO_DIAS))
    saida: dict[str, dict[str, tuple[float, str]]] = {}
    for termo, desta_busca in atuais.items():
        anteriores: dict[str, tuple[float, str]] = {}
        for chave, preco, loja in con.execute(
            "SELECT chave, preco, loja FROM precos WHERE termo = ? AND ref = 1 AND visto_em >= ? ORDER BY visto_em",
            (termo, desde),
        ):
            anteriores[chave] = (preco, loja or "")
        anteriores.update(desta_busca)
        saida[termo] = anteriores
    return saida


def montar_postagem(cand: dict) -> dict:
    p = cand["produto"]
    original = p.get("preco_original")
    if not (isinstance(original, (int, float)) and original > p["preco"]):
        original = None
    if not cand["avaliacao"].get("de_confiavel", True):
        original = None  # preço "de" inventado: não vai para o grupo nem para o site
    if cand["avaliacao"].get("de_exibir") and cand["avaliacao"]["de_exibir"] > p["preco"]:
        original = cand["avaliacao"]["de_exibir"]  # preço normal que o piloto observou
    cupom = p.get("cupom_info") or {}
    return {
        "titulo": p.get("titulo"),
        "preco": p.get("preco"),
        "preco_original": original,
        # Nome do cupom na postagem (o bot monta o bloco 🎟️ com estes campos).
        "cupom": cupom.get("codigo"),
        "cupom_desconto": cupom.get("desconto"),
        "preco_com_cupom": cupom.get("preco_com_cupom"),
        "cupom_validade": cupom.get("validade"),
        "cupom_como": cupom.get("como"),
        "cupom_minimo": cupom.get("minimo"),
        # Fim do cupom (horário de Brasília), para o site saber até quando mostrá-lo.
        "cupom_fim": cupom.get("fim"),
        "destaque": cand["avaliacao"]["destaque"],
        "link": p.get("link"),
        "imagem_url": p.get("imagem_url"),
        "plataforma": p.get("plataforma"),
        # O bot não usa estes; repassa ao site junto com a postagem (categoria
        # do catálogo e busca que encontrou a oferta).
        "categoria": p.get("categoria"),
        "termo_pesquisado": cand["termo"],
        "frete_gratis": bool(p.get("frete_gratis")),
    }


# ---------------------------------------------------------------------------
# Site: preço dos produtos já postados
# ---------------------------------------------------------------------------
def vistos_ja_postados(brutos: list[tuple[str, dict]], categoria_nome: str, con, agora: datetime) -> list[dict]:
    """
    Produtos que o piloto já postou (nos últimos 30 dias) e que este ciclo viu
    de novo. Indo para o site, o preço do catálogo acompanha a loja e a oferta
    que some das buscas sai do ar sozinha. O resto do que foi visto não vai:
    o site mostra o que passou pelos grupos.
    """
    desde = _iso(agora - timedelta(days=JANELA_HISTORICO_DIAS))
    postados = {l[0] for l in con.execute("SELECT DISTINCT chave FROM postados WHERE postado_em >= ?", (desde,))}
    if not postados:
        return []
    saida: list[dict] = []
    vistos: set[str] = set()
    for termo, item in brutos:
        ident = identificar(item.get("link", ""), item.get("plataforma", ""))
        if not ident:
            continue
        chave, link = ident
        preco = item.get("preco")
        if chave not in postados or chave in vistos or not isinstance(preco, (int, float)) or preco <= 0:
            continue
        vistos.add(chave)
        produto = dict(item)
        produto["link"] = link
        produto["termo_pesquisado"] = termo
        produto["categoria"] = categoria_nome
        saida.append(produto)
    return saida


def atualizar_site(itens: list[dict]) -> None:
    """Em segundo plano: o ciclo não espera o site (que pode estar fora do ar)."""
    if not itens:
        return
    import enviar_site

    if not enviar_site.configurado():
        return

    def _enviar():
        resumo = enviar_site.enviar_oportunidades(itens, origem="piloto")
        log(f"{enviar_site.descrever(resumo)} ({len(itens)} produto(s) já postado(s) visto(s) de novo)")

    threading.Thread(target=_enviar, name="piloto-site", daemon=True).start()


def atualizar_cupons_se_preciso(con, cfg: dict) -> bool:
    """Relê os cupons oficiais a cada PILOTO_CUPONS_HORAS. Devolve True se releu."""
    agora = _agora()
    ultima = estado_get(con, "cupons_atualizados", "")
    horas = _num(cfg, "PILOTO_CUPONS_HORAS", 0.25)
    if ultima and ultima > _iso(agora - timedelta(hours=horas)):
        return False
    _fase("buscando", "🎟️ caçando cupons da KaBuM!")
    try:
        r = cupons.atualizar_kabum(con, agora, identificar, log, int(_num(cfg, "PILOTO_CUPOM_PAGINAS", 1)))
        log(f"🎟️ Cupons KaBuM!: {r['cupons_usaveis']} válidos para ofertas "
            f"({', '.join(r['codigos'][:12]) or 'nenhum'}), {r['produtos']} produtos conferidos.")
    except Exception as exc:
        log(f"🎟️ Não consegui ler os cupons da KaBuM! agora ({exc}). Tento no próximo ciclo.")
        return False
    if str(cfg.get("PILOTO_CUPONS_ML")).strip() != "0":
        _fase("buscando", "🎟️ caçando cupons do Mercado Livre")
        try:
            r = cupons.atualizar_ml(con, agora, cfg.get("PILOTO_CUPONS_ML_CATEGORIAS") or cupons.ML_CATEGORIAS_PADRAO)
            estado_set(con, "ml_sessao", "ok")
            log(f"🎟️ Cupons Mercado Livre: {r['cupons_ativos']} cupons gerais de tecnologia "
                f"({'; '.join(r['titulos'][:6]) or 'nenhum'}).")
        except cupons.SessaoMLExpirada as exc:
            estado_set(con, "ml_sessao", str(exc))
            log(f"🎟️ Mercado Livre: {exc}")
        except Exception as exc:
            log(f"🎟️ Não consegui ler os cupons do Mercado Livre agora ({exc}).")
    estado_set(con, "cupons_atualizados", _iso(agora))
    return True


def sincronizar_cupons_do_site(con) -> None:
    """Cupons cadastrados no painel do site entram na fila de avisos. Falha não para o ciclo."""
    try:
        r = cupons.sincronizar_do_site(con, _agora())
    except Exception as exc:
        log(f"🎟️ Não consegui buscar os cupons do painel do site ({exc}).")
        return
    if r is None:
        return
    # Só escreve no log quando a fila do painel muda (o ciclo roda a cada poucos minutos).
    resumo = f"{r['cupons']}:{','.join(r['lojas'])}"
    if resumo != estado_get(con, "cupons_site", ""):
        estado_set(con, "cupons_site", resumo)
        if r["cupons"]:
            log(f"🎟️ Cupons do painel do site esperando divulgação: {r['cupons']} ({', '.join(r['lojas'])}).")


def postar_aviso_de_cupom(con, cfg: dict) -> bool:
    """Um anúncio de cupom (código, desconto, compra mínima, validade e link afiliado da loja)."""
    agora = _agora()
    if postados_na_ultima_hora(con, agora) >= int(_num(cfg, "PILOTO_MAX_POR_HORA")):
        return False
    cupom = cupons.proximo_aviso(con, agora, _num(cfg, "PILOTO_AVISO_CUPOM_INTERVALO"),
                                 int(_num(cfg, "PILOTO_AVISOS_CUPOM_POR_DIA")))
    if not cupom:
        return False
    # Cupom da planilha da KaBuM! é reconferido; o cadastrado por você (portal
    # ou painel do site) já foi conferido por você.
    if (cupom["loja"] == "KaBuM!" and cupom.get("fonte") not in ("site", "manual")
            and not cupons.ainda_valido({"origem": "lista oficial", **cupom}, agora)):
        return False
    return _publicar_aviso(con, cupom)[0]


def divulgar_cupom_manual(ident: int) -> tuple[bool, str]:
    """Botão "Divulgar agora" da aba Cupons: posta o aviso na hora, fora da fila."""
    con = conectar()
    m = cupons.manual_por_id(con, ident, _agora())
    if not m:
        return False, "cupom não encontrado"
    if m["situacao"] == "vencido":
        return False, "cupom vencido"
    with _trava_estado:
        anterior = (_estado["fase"], _estado["detalhe"])
    try:
        return _publicar_aviso(con, cupons.manual_como_cupom(m))
    finally:
        with _trava_estado:
            if _estado["fase"] == "publicando":   # devolve o status do piloto ao que era
                _estado["fase"], _estado["detalhe"] = anterior


def _publicar_aviso(con, cupom: dict) -> tuple[bool, str]:
    pedido = cupons.montar_aviso(cupom)
    _fase("publicando", pedido["titulo"][:80])
    ok, detalhe = publicar(pedido)
    if not ok:
        log(f"❌ Aviso de cupom falhou: {pedido['titulo']} — {detalhe}")
        return False, detalhe
    cupons.registrar_aviso(con, cupom, detalhe, _agora())
    con.execute(
        "INSERT INTO postados (chave, plataforma, titulo, preco, nota, link, canais, postado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (f"cupom:{cupom['loja']}:{cupom['codigo']}", cupom["loja"], pedido["titulo"], None, None,
         cupom["link"], detalhe, _iso(_agora())),
    )
    con.commit()
    log(f"🎟️ Aviso de cupom postado em {detalhe}: {pedido['titulo']} "
        f"({'compra mínima ' + cupons.brl(cupom['minimo']) if cupom.get('minimo') else 'sem compra mínima'})")
    return True, detalhe


def rodar_ciclo(trava: threading.Lock | None = None, con=None) -> dict:
    cfg = ler_config()
    con = con or conectar()
    inicio = _agora()
    silencio = em_silencio(cfg, inicio)
    resumo = {"categorias": [], "vistos": 0, "candidatos": 0, "publicados": 0, "silencio": silencio}
    cupons_ligados = str(cfg.get("PILOTO_CUPONS")).strip() != "0"
    cupons_novos = cupons_ligados and atualizar_cupons_se_preciso(con, cfg)
    if str(cfg.get("PILOTO_APRENDIZADO")).strip() != "0":
        try:
            novos = aprendizado.recalcular_se_preciso(con, inicio)
            if novos:
                log("🧠 Aprendizado: " + aprendizado.descrever(novos))
        except Exception as exc:
            log(f"🧠 Aprendizado: falha ao recalcular ({exc}). Sigo com o que já sabia.")

    for indice_cat, chave_cat in enumerate(proximas_categorias(con, cfg)):
        import categories

        info = categories.CATEGORIAS[chave_cat]
        with _trava_estado:
            _estado["ultima_categoria"] = f"{info['emoji']} {info['nome']}"
        log(f"Ciclo: {info['emoji']} {info['nome']}" + (" (horário de silêncio: só coleta preços)" if silencio else ""))

        brutos = buscar_categoria(chave_cat, cfg, trava)
        agora = _agora()
        candidatos = processar(brutos, info["nome"], con, cfg, agora)
        if cupons_ligados and indice_cat == 0:
            # Ofertas puxadas das listas oficiais de cupom concorrem junto com
            # as da categoria (no máximo PILOTO_CUPOM_POSTS_POR_CICLO por ciclo).
            de_cupom = processar(cupons.candidatos_de_cupons(con, agora), "Cupons", con, cfg, agora,
                                 registrar_precos=cupons_novos)
            if de_cupom:
                log(f"{len(de_cupom)} oferta(s) das listas de cupom passaram nas regras.")
            candidatos += de_cupom
        if str(cfg.get("PILOTO_ATUALIZAR_SITE")).strip() != "0":
            atualizar_site(vistos_ja_postados(brutos, info["nome"], con, agora))
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
            info_cupom = p.get("cupom_info")
            if info_cupom and not cupons.ainda_valido(info_cupom, _agora()):
                log(f"Cupom {info_cupom.get('codigo')} expirou antes da postagem: pulei {p.get('titulo', '')[:50]}")
                continue
            _fase("publicando", p.get("titulo", "")[:80])
            ok, detalhe = publicar(montar_postagem(cand))
            if ok:
                con.execute(
                    "INSERT INTO postados (chave, plataforma, titulo, preco, nota, link, canais, postado_em) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (cand["chave"], p.get("plataforma"), p.get("titulo"),
                     cand["avaliacao"].get("preco_final") or p.get("preco"),
                     cand["avaliacao"]["nota"], p.get("link"), detalhe, _iso(_agora())),
                )
                con.commit()
                resumo["publicados"] += 1
                txt_cupom = ""
                if info_cupom:
                    txt_cupom = f" + cupom {info_cupom.get('codigo') or info_cupom.get('desconto')}"
                    if info_cupom.get("preco_com_cupom"):
                        txt_cupom += f" = R$ {info_cupom['preco_com_cupom']:.2f}"
                log(f"✅ Postado em {detalhe}: [{p.get('plataforma')}] {p.get('titulo', '')[:70]} "
                    f"R$ {p['preco']:.2f}{txt_cupom} ({cand['avaliacao']['destaque']}, nota {cand['avaliacao']['nota']})")
            else:
                log(f"❌ Falhou: {p.get('titulo', '')[:60]} — {detalhe}")
            if indice < len(escolhidos) - 1:
                time.sleep(random.uniform(gap_min, gap_max))

    # Anúncio de cupom: no máximo 1 por ciclo, respeitando o intervalo e o
    # limite diário (PILOTO_AVISO_CUPOM_INTERVALO / PILOTO_AVISOS_CUPOM_POR_DIA).
    if cupons_ligados and not silencio and estado_get(con, "pausado", "0") != "1":
        sincronizar_cupons_do_site(con)
        try:
            if resumo["publicados"]:
                time.sleep(random.uniform(float(os.getenv("PUBLISH_MIN_DELAY", "6")),
                                          float(os.getenv("PUBLISH_MAX_DELAY", "14"))))
            if postar_aviso_de_cupom(con, cfg):
                resumo["publicados"] += 1
        except Exception as exc:
            log(f"🎟️ Erro no aviso de cupom: {exc}")

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
        "cupons": cupons.resumo(con, agora),
        "ml_sessao": estado_get(con, "ml_sessao", ""),
        "aprendizado": aprendizado.carregar(con),
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
