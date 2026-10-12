"""
Caçador de cupons: acha cupons válidos e confere em quais ofertas eles valem.

KaBuM! (fonte completa)
    A página oficial kabum.com.br/hotsite/cupons é desenhada a partir de uma
    planilha pública do Google que a própria KaBuM! mantém: código, desconto,
    início, fim, condições e o link "Ver ofertas". Esse link aponta para
    /promocao/<slug> (a lista exata de produtos em que o cupom vale) ou para um
    produto só. O caçador lê a planilha, descarta o que expirou ou ainda não
    começou, baixa a lista de produtos de cada cupom e guarda tudo em
    data/piloto.db. Uma oferta só recebe o cupom se o produto estiver nessa
    lista — é a verificação de "vale para esta oferta".

Mercado Livre (central de cupons, com login)
    A central mercadolivre.com.br/cupons só abre logada e os cupons não têm
    código: são campanhas que cada pessoa "Aplica" na própria conta, com compra
    mínima, limite de desconto e validade. O caçador lê as categorias de
    tecnologia com a sessão salva pelo LOGIN-MERCADOLIVRE.bat e guarda só os
    cupons gerais de campanha (os de loja seguida são da conta, não do grupo).
    Eles viram postagens de cupom com o link de afiliado da central de cupons.

Amazon, AliExpress (e o cupom do anúncio em todas)
    Não há lista pública de cupons por código (na Amazon os cupons são de
    "ativar na página", sem código). O que existe é o cupom impresso no próprio
    anúncio, que a busca já captura. Ele é interpretado aqui (valor,
    porcentagem, código quando houver) e entra no preço final e na postagem.

Painel do site (qualquer loja)
    Cupom que você cadastra em Tech Lobo > Admin > Cupons (Amazon, AliExpress,
    Shopee, Magalu...) chega por /api/integracao/cupons a cada ciclo e entra na
    fila de avisos na frente dos demais. Sai uma vez; depois de divulgado, o
    site marca e ele some da lista.

Cupons manuais (qualquer loja, pelo portal)
    Aba "Cupons" do portal: código (ou "sem código"), desconto, compra
    mínima, validade e link. Entram na fila de avisos na frente dos demais
    (ou saem na hora com "Divulgar agora"). Marcando "aplicar nas ofertas",
    o cupom também entra no preço final e na postagem das ofertas daquela
    loja — de todas ou só das que têm certas palavras no título.

Nada aqui testa o cupom num carrinho: isso exigiria uma conta logada fazendo
compras de mentira, o que arrisca bloquear a conta. A garantia vem de usar só a
fonte oficial e conferir a validade de novo logo antes de postar.
"""

from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime, timedelta, timezone

import requests

PLANILHA_KABUM = (
    "https://docs.google.com/spreadsheets/d/1Hi1d81MW60rycvtmE8jFaFdekrlgndbvXrWglu33RtA/gviz/tq?tqx=out:json"
)
PROMOCAO_KABUM = "https://www.kabum.com.br/promocao/{slug}?page_number={pagina}&page_size=100"

BRASILIA = timezone(timedelta(hours=-3))

# Condições que o piloto não consegue garantir numa oferta isolada.
RESTRICOES = [
    (re.compile(r"primeira\s+compra", re.I), "só na primeira compra"),
    (re.compile(r"m[ií]nimo\s+(?:de\s+)?\d+\s+produtos", re.I), "exige vários produtos no carrinho"),
    (re.compile(r"(?:exclusiv[oa]|somente|apenas|s[óo])\s+(?:no|para\s+o|pelo)\s+app", re.I), "só no app"),
]
MINIMO_RE = re.compile(r"(?:acima\s+de|a\s+partir\s+de|m[ií]nimo\s+de)\s+R\$\s*([\d.]+(?:,\d{1,2})?)", re.I)

_cache_planilha: dict = {"quando": 0.0, "cupons": None}


# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------
def _num_br(texto: str) -> float | None:
    texto = (texto or "").strip()
    if not texto:
        return None
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", texto):
        texto = texto.replace(".", "")  # '2.499' é dois mil e quatrocentos
    try:
        return float(texto)
    except ValueError:
        return None


def interpretar_desconto(texto: str) -> tuple[str, float] | None:
    """'10% OFF' -> ('pct', 10); 'R$100 OFF' / 'R$ 1.000,00' -> ('valor', 100.0)."""
    texto = texto or ""
    pct = re.search(r"(\d{1,2}(?:[.,]\d+)?)\s*%", texto)
    if pct:
        return "pct", float(pct.group(1).replace(",", "."))
    valor = re.search(r"R\$\s*([\d.]+(?:,\d{1,2})?)", texto)
    if valor:
        numero = _num_br(valor.group(1))
        if numero:
            return "valor", numero
    return None


def aplicar(preco: float, tipo: str, valor: float) -> float:
    if tipo not in ("pct", "valor") or not valor:
        return preco
    if tipo == "pct":
        return round(preco * (1 - valor / 100), 2)
    return round(max(0.0, preco - valor), 2)


def texto_desconto(tipo: str, valor: float) -> str:
    if tipo == "frete":
        return "FRETE GRÁTIS"
    if tipo == "pct":
        return f"{valor:g}% OFF"
    return f"R$ {valor:,.2f} OFF".replace(",", "X").replace(".", ",").replace("X", ".").replace(",00 OFF", " OFF")


def _data(valor) -> datetime | None:
    """Datas da planilha: ISO com fuso ('2026-10-13T09:00:00-03:00') ou 'Date(2026,9,13,9,0,0)'."""
    if not valor:
        return None
    texto = str(valor)
    achado = re.match(r"Date\((\d+),(\d+),(\d+)(?:,(\d+),(\d+)(?:,(\d+))?)?\)", texto)
    if achado:
        partes = [int(p) if p else 0 for p in achado.groups()]
        return datetime(partes[0], partes[1] + 1, partes[2], partes[3], partes[4], partes[5])
    try:
        momento = datetime.fromisoformat(texto)
    except ValueError:
        return None
    if momento.tzinfo:
        momento = momento.astimezone(BRASILIA).replace(tzinfo=None)
    return momento


def _iso(momento: datetime) -> str:
    return momento.strftime("%Y-%m-%d %H:%M:%S")


def validade_legivel(fim: str | None) -> str | None:
    if not fim:
        return None
    try:
        momento = datetime.strptime(fim, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    hora = f"{momento:%H}h" + (f"{momento:%M}" if momento.minute else "")
    return f"{momento:%d/%m} às {hora}"


# ---------------------------------------------------------------------------
# Banco
# ---------------------------------------------------------------------------
def preparar_tabelas(con) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS cupons (
            loja TEXT NOT NULL,
            codigo TEXT NOT NULL,
            tipo TEXT,
            valor REAL,
            descricao TEXT,
            link TEXT,
            inicio TEXT,
            fim TEXT,
            condicoes TEXT,
            restricao TEXT,
            minimo REAL,
            atualizado_em TEXT,
            PRIMARY KEY (loja, codigo)
        );
        CREATE TABLE IF NOT EXISTS cupom_produtos (
            loja TEXT NOT NULL,
            codigo TEXT NOT NULL,
            chave TEXT NOT NULL,
            titulo TEXT,
            preco REAL,
            preco_original REAL,
            link TEXT,
            imagem_url TEXT,
            frete_gratis INTEGER,
            atualizado_em TEXT,
            PRIMARY KEY (loja, codigo, chave)
        );
        CREATE INDEX IF NOT EXISTS idx_cupom_produtos_chave ON cupom_produtos(chave);
        CREATE TABLE IF NOT EXISTS cupons_postados (
            loja TEXT NOT NULL,
            codigo TEXT NOT NULL,
            postado_em TEXT NOT NULL,
            canais TEXT
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS cupons_manuais (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loja TEXT NOT NULL,
            codigo TEXT,
            sem_codigo INTEGER DEFAULT 0,
            como TEXT,
            tipo TEXT NOT NULL,
            valor REAL,
            minimo REAL DEFAULT 0,
            limite REAL,
            inicio TEXT,
            fim TEXT,
            link TEXT,
            titulo TEXT,
            descricao TEXT,
            aplicar_ofertas INTEGER DEFAULT 0,
            palavras TEXT,
            divulgar INTEGER DEFAULT 1,
            criado_em TEXT,
            atualizado_em TEXT
        )
        """
    )
    colunas = {linha[1] for linha in con.execute("PRAGMA table_info(cupons)")}
    for coluna, tipo in (("limite", "REAL"), ("imagem", "TEXT"), ("titulo", "TEXT"), ("fonte", "TEXT"),
                        ("id_site", "INTEGER"), ("como", "TEXT"), ("sem_codigo", "INTEGER")):
        if coluna not in colunas:
            con.execute(f"ALTER TABLE cupons ADD COLUMN {coluna} {tipo}")


# ---------------------------------------------------------------------------
# KaBuM!: planilha oficial
# ---------------------------------------------------------------------------
def _headers() -> dict:
    try:
        import antibot

        return antibot.http_headers()
    except Exception:
        return {"User-Agent": "Mozilla/5.0", "Accept-Language": "pt-BR,pt;q=0.9"}


def ler_planilha_kabum(timeout: int = 20) -> list[dict]:
    resposta = requests.get(PLANILHA_KABUM, headers=_headers(), timeout=timeout)
    resposta.raise_for_status()
    texto = resposta.text
    inicio, fim = texto.find("{"), texto.rfind("}")
    dados = json.loads(texto[inicio:fim + 1])
    cupons = []
    for linha in dados.get("table", {}).get("rows", []):
        celulas = [(c or {}).get("v") if isinstance(c, dict) else None for c in (linha.get("c") or [])]
        celulas += [None] * (9 - len(celulas))
        codigo = str(celulas[3] or "").strip().upper()
        if not codigo or codigo == "CUPOM":
            continue  # cabeçalho
        desconto = interpretar_desconto(str(celulas[4] or ""))
        condicoes = str(celulas[7] or "")
        restricao = next((motivo for regra, motivo in RESTRICOES if regra.search(condicoes)), None)
        minimo = MINIMO_RE.search(condicoes)
        cupons.append({
            "loja": "KaBuM!",
            "codigo": codigo,
            "tipo": desconto[0] if desconto else None,
            "valor": desconto[1] if desconto else None,
            "descricao": f"{str(celulas[4] or '').strip()} {str(celulas[5] or '').strip()}".strip(),
            "link": str(celulas[6] or "").strip(),
            "inicio": _data(celulas[1]),
            "fim": _data(celulas[2]),
            "condicoes": condicoes,
            "restricao": restricao,
            "minimo": _num_br(minimo.group(1)) if minimo else 0.0,
            "imagem": str(celulas[0] or "").strip() or None,
        })
    return cupons


def cupons_kabum_cache(max_idade_s: int = 600) -> list[dict]:
    """Planilha relida no máximo a cada 10 min: usada para reconferir antes de postar."""
    agora = time.time()
    if _cache_planilha["cupons"] is None or agora - _cache_planilha["quando"] > max_idade_s:
        _cache_planilha["cupons"] = ler_planilha_kabum()
        _cache_planilha["quando"] = agora
    return _cache_planilha["cupons"]


def ativo(cupom: dict, agora: datetime) -> bool:
    inicio, fim = cupom.get("inicio"), cupom.get("fim")
    if isinstance(inicio, str):
        inicio = datetime.strptime(inicio, "%Y-%m-%d %H:%M:%S") if inicio else None
    if isinstance(fim, str):
        fim = datetime.strptime(fim, "%Y-%m-%d %H:%M:%S") if fim else None
    if not fim:
        # Cupom cadastrado no painel pode não ter validade: vale enquanto o site o listar.
        return cupom.get("fonte") == "site" and (not inicio or inicio <= agora)
    if fim <= agora:
        return False
    return not inicio or inicio <= agora


def _produtos_da_promocao(slug: str, max_paginas: int) -> list[dict]:
    from kabum_scraper import KabumScraper

    leitor = KabumScraper(headless=True)
    produtos: list[dict] = []
    vistos: set[str] = set()
    sessao = requests.Session()
    for pagina in range(1, max_paginas + 1):
        resposta = sessao.get(PROMOCAO_KABUM.format(slug=slug, pagina=pagina), headers=_headers(), timeout=30)
        if resposta.status_code != 200:
            break
        itens = leitor.parse_html(resposta.text)
        novos = [i for i in itens if i.get("link") and i["link"] not in vistos]
        if not novos:
            break
        for item in novos:
            vistos.add(item["link"])
        produtos.extend(novos)
        if len(itens) < 100:
            break
        time.sleep(1.0)
    sessao.close()
    return produtos


def _produto_unico(url: str) -> list[dict]:
    resposta = requests.get(url, headers=_headers(), timeout=30)
    if resposta.status_code != 200:
        return []
    achado = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resposta.text, re.S)
    if not achado:
        return []
    produto = json.loads(achado.group(1)).get("props", {}).get("pageProps", {}).get("product") or {}
    if not isinstance(produto, dict) or not produto.get("id") or produto.get("available") is False:
        return []
    precos = produto.get("prices") or {}
    preco = precos.get("priceWithDiscount") or precos.get("price") or produto.get("price")
    antigo = precos.get("oldPrice")
    return [{
        "plataforma": "KaBuM!",
        "titulo": produto.get("title"),
        "preco": float(preco) if preco else None,
        "preco_original": float(antigo) if antigo and preco and antigo > preco else None,
        "link": f"https://www.kabum.com.br/produto/{produto['id']}/{produto.get('friendlyName') or ''}",
        "imagem_url": produto.get("thumbnail"),
        "frete_gratis": bool((produto.get("flags") or {}).get("isFreeShipping")),
        "condicao": "Novo",
    }]


def _gravar_cupom(con, c: dict, agora: datetime) -> None:
    con.execute(
        "INSERT OR REPLACE INTO cupons (loja, codigo, tipo, valor, descricao, link, inicio, fim, condicoes, "
        "restricao, minimo, atualizado_em, limite, imagem, titulo) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (c["loja"], c["codigo"], c["tipo"], c["valor"], c["descricao"], c["link"],
         _iso(c["inicio"]) if c.get("inicio") else None, _iso(c["fim"]) if c.get("fim") else None,
         c.get("condicoes"), c.get("restricao"), c.get("minimo"), _iso(agora),
         c.get("limite"), c.get("imagem"), c.get("titulo")),
    )


def atualizar_kabum(con, agora: datetime, identificar, log, max_paginas: int = 10) -> dict:
    """Relê a planilha e a lista de produtos de cada cupom ativo. Devolve um resumo."""
    preparar_tabelas(con)
    cupons = ler_planilha_kabum()
    _cache_planilha.update(quando=time.time(), cupons=cupons)

    ativos = [c for c in cupons if ativo(c, agora) and c.get("tipo")]
    con.execute("DELETE FROM cupons WHERE loja = 'KaBuM!'")
    for c in cupons:
        _gravar_cupom(con, c, agora)

    total_produtos = 0
    con.execute("DELETE FROM cupom_produtos WHERE loja = 'KaBuM!'")
    for c in ativos:
        if c["restricao"]:
            continue
        link = c["link"]
        try:
            if "/promocao/" in link:
                slug = link.rstrip("/").split("/promocao/")[1].split("?")[0]
                itens = _produtos_da_promocao(slug, max_paginas)
            elif "/produto/" in link:
                itens = _produto_unico(link)
            else:
                itens = []  # cupom genérico sem lista: não dá para garantir em qual produto vale
        except Exception as exc:
            log(f"Cupom {c['codigo']}: não consegui ler os produtos ({exc}).")
            continue
        for item in itens:
            ident = identificar(item.get("link", ""), "KaBuM!")
            if not ident or not item.get("preco"):
                continue
            chave, link_limpo = ident
            con.execute(
                "INSERT OR REPLACE INTO cupom_produtos (loja, codigo, chave, titulo, preco, preco_original, link, "
                "imagem_url, frete_gratis, atualizado_em) VALUES ('KaBuM!', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (c["codigo"], chave, item.get("titulo"), item.get("preco"), item.get("preco_original"),
                 link_limpo, item.get("imagem_url"), int(bool(item.get("frete_gratis"))), _iso(agora)),
            )
            total_produtos += 1
    con.commit()
    usaveis = [c for c in ativos if not c["restricao"]]
    return {
        "cupons_ativos": len(ativos),
        "cupons_usaveis": len(usaveis),
        "produtos": total_produtos,
        "codigos": [c["codigo"] for c in usaveis],
    }


# ---------------------------------------------------------------------------
# Consulta: qual cupom vale para esta oferta?
# ---------------------------------------------------------------------------
def _linha_cupom(row) -> dict:
    nomes = ["loja", "codigo", "tipo", "valor", "descricao", "link", "inicio", "fim", "condicoes", "restricao", "minimo"]
    return dict(zip(nomes, row))


def cupom_da_lista(con, chave: str, preco: float, agora: datetime) -> dict | None:
    """Melhor cupom oficial em cuja lista de produtos esta oferta está."""
    preparar_tabelas(con)
    linhas = con.execute(
        "SELECT c.loja, c.codigo, c.tipo, c.valor, c.descricao, c.link, c.inicio, c.fim, c.condicoes, "
        "c.restricao, c.minimo FROM cupom_produtos p JOIN cupons c ON c.loja = p.loja AND c.codigo = p.codigo "
        "WHERE p.chave = ? AND c.restricao IS NULL AND c.tipo IS NOT NULL",
        (chave,),
    ).fetchall()
    melhor = None
    for row in linhas:
        cupom = _linha_cupom(row)
        if not ativo(cupom, agora):
            continue
        if cupom["minimo"] and preco < cupom["minimo"]:
            continue
        final = aplicar(preco, cupom["tipo"], cupom["valor"])
        if final <= 0 or final >= preco:
            continue
        if not melhor or final < melhor["preco_com_cupom"]:
            melhor = {
                "codigo": cupom["codigo"],
                "desconto": texto_desconto(cupom["tipo"], cupom["valor"]),
                "preco_com_cupom": final,
                "validade": validade_legivel(cupom["fim"]),
                "fim": cupom["fim"],
                "origem": "lista oficial",
                "como": None,
                "minimo": cupom["minimo"] or 0.0,
            }
    return melhor


PALAVRAS_NAO_CODIGO = {
    "CUPOM", "OFF", "DESCONTO", "COUPON", "USE", "GANHE", "ECONOMIZE", "PRIME", "FRETE", "GRATIS",
    "GRÁTIS", "PIX", "OFERTA", "COMPRA", "PARA", "APP", "SITE", "LOJA", "ITEM", "ITENS", "MAIS",
}


def cupom_do_anuncio(texto: str | None, preco: float, plataforma: str = "") -> dict | None:
    """
    Interpreta o cupom impresso no anúncio:
      'Economize R$ 50,00 com cupom' (Amazon) · 'CUPOM GAMER10' (KaBuM!)
      'Cupom 10% OFF' (Mercado Livre) · 'R$ 15 off em R$ 100' (AliExpress)
    """
    if not texto or not re.search(r"cupom|coupon|economize|c[oó]digo", texto, re.I):
        return None
    codigo = None
    # Código é uma palavra toda em maiúsculas com ao menos uma letra
    # ('GAMER10', 'NINTENDOU'); 'OFF', 'CUPOM' e afins não contam.
    for token in re.findall(r"\b[A-Z0-9]{4,20}\b", texto):
        if token not in PALAVRAS_NAO_CODIGO and re.search(r"[A-Z]", token):
            codigo = token
            break
    desconto = interpretar_desconto(texto)
    minimo = re.search(r"(?:em|acima de|a partir de)\s+R\$\s*([\d.]+(?:,\d{1,2})?)", texto, re.I)
    if desconto and desconto[0] == "valor" and minimo:
        # 'R$ 15 off em R$ 100': o primeiro R$ é o desconto, o segundo o mínimo.
        valores = re.findall(r"R\$\s*([\d.]+(?:,\d{1,2})?)", texto)
        if len(valores) >= 2:
            desconto = ("valor", _num_br(valores[0]) or desconto[1])
        if preco < (_num_br(minimo.group(1)) or 0):
            desconto = None
    if not codigo and not desconto:
        return None
    final = aplicar(preco, *desconto) if desconto else None
    if final is not None and (final <= 0 or final >= preco):
        final = None
    # Sem código, o cupom é do tipo "ativar/coletar" na página do produto.
    como = None if codigo else "ative o cupom na página do produto"
    return {
        "codigo": codigo,
        "desconto": texto_desconto(*desconto) if desconto else None,
        "preco_com_cupom": final,
        "validade": None,
        "fim": None,
        "origem": "anúncio",
        "como": como,
    }


def melhor_cupom(con, chave: str, item: dict, agora: datetime) -> dict | None:
    """Cupom oficial da lista (KaBuM!) tem prioridade; senão, o do anúncio."""
    preco = item.get("preco")
    if not isinstance(preco, (int, float)) or preco <= 0:
        return None
    oficial = cupom_da_lista(con, chave, float(preco), agora) if chave.startswith("kabum:") else None
    anuncio = cupom_do_anuncio(item.get("cupom"), float(preco), item.get("plataforma", ""))

    # O selo 'CUPOM NINTENDOU' da KaBuM! traz só o nome: completa com a planilha.
    if anuncio and anuncio.get("codigo") and chave.startswith("kabum:") and not anuncio.get("preco_com_cupom"):
        try:
            for c in cupons_kabum_cache():
                if c["codigo"] == anuncio["codigo"] and c.get("tipo") and not c.get("restricao") and ativo(c, agora):
                    final = aplicar(float(preco), c["tipo"], c["valor"])
                    if 0 < final < preco and not (c.get("minimo") and preco < c["minimo"]):
                        anuncio.update(desconto=texto_desconto(c["tipo"], c["valor"]), preco_com_cupom=final,
                                       validade=validade_legivel(_iso(c["fim"])), fim=_iso(c["fim"]),
                                       origem="lista oficial", como=None, minimo=c.get("minimo") or 0.0)
                    break
        except Exception:
            pass

    manual = cupom_manual_para_oferta(con, chave, item, float(preco), agora)

    opcoes = [c for c in (oficial, anuncio, manual) if c]
    if not opcoes:
        return None
    return min(opcoes, key=lambda c: c["preco_com_cupom"] if c.get("preco_com_cupom") else float("inf"))


def ainda_valido(cupom: dict, agora: datetime) -> bool:
    """Reconfere na planilha (cache de 10 min) logo antes de postar."""
    if cupom.get("origem") == "manual":
        return not cupom.get("fim") or cupom["fim"] > _iso(agora)
    if cupom.get("origem") != "lista oficial":
        return True
    try:
        for c in cupons_kabum_cache():
            if c["codigo"] == cupom["codigo"]:
                return ativo(c, agora) and not c.get("restricao")
    except Exception:
        # Planilha fora do ar: confia na validade já gravada.
        return bool(cupom.get("fim")) and cupom["fim"] > _iso(agora)
    return False


def candidatos_de_cupons(con, agora: datetime, por_cupom: int = 15) -> list[tuple[str, dict]]:
    """
    Produtos das listas de cupom, como se tivessem vindo de uma busca: o termo
    é 'cupom CODIGO'. Por cupom, só os que mais economizam com o cupom aplicado.
    """
    preparar_tabelas(con)
    saida: list[tuple[str, dict]] = []
    cupons = con.execute(
        "SELECT loja, codigo, tipo, valor, descricao, link, inicio, fim, condicoes, restricao, minimo "
        "FROM cupons WHERE restricao IS NULL AND tipo IS NOT NULL AND loja = 'KaBuM!'"
    ).fetchall()
    for row in cupons:
        cupom = _linha_cupom(row)
        if not ativo(cupom, agora):
            continue
        produtos = con.execute(
            "SELECT titulo, preco, preco_original, link, imagem_url, frete_gratis FROM cupom_produtos "
            "WHERE loja = ? AND codigo = ?",
            (cupom["loja"], cupom["codigo"]),
        ).fetchall()

        def economia(p):
            preco, original = p[1] or 0, p[2] or p[1] or 0
            final = aplicar(preco, cupom["tipo"], cupom["valor"])
            return (original - final) / original if original else 0

        for p in sorted(produtos, key=economia, reverse=True)[:por_cupom]:
            saida.append((f"cupom {cupom['codigo']}", {
                "plataforma": cupom["loja"],
                "titulo": p[0],
                "preco": p[1],
                "preco_original": p[2],
                "link": p[3],
                "imagem_url": p[4],
                "frete_gratis": bool(p[5]),
                "condicao": "Novo",
                "cupom": f"CUPOM {cupom['codigo']}",
            }))
    return saida


def resumo(con, agora: datetime) -> dict:
    preparar_tabelas(con)
    linhas = con.execute(
        "SELECT loja, codigo, tipo, valor, descricao, link, inicio, fim, condicoes, restricao, minimo, atualizado_em, titulo "
        "FROM cupons"
    ).fetchall()
    ativos = [dict(_linha_cupom(l[:11]), atualizado_em=l[11], titulo=l[12]) for l in linhas
              if ativo(_linha_cupom(l[:11]), agora)]
    contagem = dict(con.execute("SELECT codigo, COUNT(*) FROM cupom_produtos GROUP BY codigo").fetchall())
    manuais = [m for m in listar_manuais(con, agora) if m["situacao"] == "ativo"]
    return {
        "ativos": [
            {"codigo": c["titulo"] if c["loja"] == "Mercado Livre" else c["codigo"], "loja": c["loja"],
             "descricao": c["descricao"], "validade": validade_legivel(c["fim"]), "minimo": c["minimo"],
             "restricao": c["restricao"], "produtos": contagem.get(c["codigo"], 0)}
            for c in sorted(ativos, key=lambda c: c["fim"] or "")
        ] + [
            {"codigo": "sem código" if m["sem_codigo"] else m["codigo"], "loja": m["loja"], "manual": True,
             "descricao": f"{texto_desconto(m['tipo'], m['valor'])} {m.get('titulo') or ''}".strip(),
             "validade": validade_legivel(m["fim"]), "minimo": m["minimo"], "restricao": None, "produtos": 0}
            for m in manuais
        ],
        "atualizado_em": max((c["atualizado_em"] or "" for c in ativos), default=None),
    }


# ---------------------------------------------------------------------------
# Mercado Livre: central de cupons (exige a sessão do LOGIN-MERCADOLIVRE.bat)
# ---------------------------------------------------------------------------
ML_CUPONS = "https://www.mercadolivre.com.br/cupons/filter?category={categoria}"
ML_CENTRAL = "https://www.mercadolivre.com.br/cupons"
ML_CATEGORIAS_PADRAO = "computing,consumer_electronics,cellphones_and_phones,games,camera_and_accesories"


class SessaoMLExpirada(Exception):
    pass


def caminho_sessao_ml() -> str:
    import os

    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "profiles", "mercadolivre_cupons.json")


def _cookies_ml() -> requests.cookies.RequestsCookieJar | None:
    import os

    caminho = caminho_sessao_ml()
    if not os.path.exists(caminho):
        return None
    with open(caminho, encoding="utf-8") as arquivo:
        estado = json.load(arquivo)
    pote = requests.cookies.RequestsCookieJar()
    for c in estado.get("cookies", []):
        if "mercadoli" in (c.get("domain") or ""):
            pote.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path") or "/")
    return pote if len(pote) else None


def _objetos_cupom(no):
    if isinstance(no, dict):
        if "campaignId" in no and "activationType" in no:
            yield no
        for valor in no.values():
            yield from _objetos_cupom(valor)
    elif isinstance(no, list):
        for valor in no:
            yield from _objetos_cupom(valor)


def _contexto_ml(html: str) -> dict:
    marca = "_n.ctx.r="
    inicio = html.find(marca)
    if inicio < 0:
        raise ValueError("página de cupons sem os dados esperados")
    objeto, _ = json.JSONDecoder().raw_decode(html[inicio + len(marca):])
    return objeto


def _valor_ml(bloco: dict | None, texto: str | None) -> float | None:
    if bloco and bloco.get("fractionalAmount") is not None:
        inteiro = _num_br(str(bloco.get("fractionalAmount")))
        centavos = str(bloco.get("decimalAmount") or "0")
        if inteiro is not None:
            return inteiro + (float("0." + centavos) if centavos.strip("0") else 0.0)
    if texto:
        achado = re.search(r"R\$\s*([\d.]+(?:,\d{1,2})?)", texto)
        if achado:
            return _num_br(achado.group(1))
    return None


def interpretar_cupom_ml(obj: dict) -> dict | None:
    """Só cupons gerais de campanha ('Aplicar', em produtos selecionados)."""
    if obj.get("activationType") != "EU_QUERO":
        return None  # 'AUTOMATIC' = cupom de loja seguida, é da conta e não do grupo
    titulo = ((obj.get("title") or {}).get("text") or "").strip()
    subtitulo = ((obj.get("initialSubtitle") or {}).get("text") or "").strip()
    desconto = interpretar_desconto(titulo)
    if not titulo or not desconto:
        return None
    futuro = obj.get("futureCouponInfo") or {}
    fim = _data(futuro.get("expiration_date"))
    inicio = _data(futuro.get("start_date"))
    valores = obj.get("amount") or {}
    acess = valores.get("accessibility") or {}
    minimo_txt = valores.get("minAmount") or ""
    minimo = 0.0 if not minimo_txt or "sem compra" in minimo_txt.lower() else _valor_ml(acess.get("minAmount"), minimo_txt)
    limite = _valor_ml(acess.get("capAmount"), valores.get("capAmount"))
    itens = obj.get("items") or []
    imagem = (itens[0].get("imageUrl") if itens and isinstance(itens[0], dict) else None) or obj.get("iconUrl")
    if imagem and imagem.endswith(".webp"):
        imagem = imagem[:-5] + ".jpg"  # o Telegram e o WhatsApp lidam melhor com JPG; o mlstatic serve os dois
    return {
        "loja": "Mercado Livre",
        "codigo": f"ML{obj.get('campaignId')}",
        "titulo": titulo,
        "tipo": desconto[0],
        "valor": desconto[1],
        "descricao": f"{titulo} · {subtitulo}".strip(" ·"),
        "link": ML_CENTRAL,
        "inicio": inicio,
        "fim": fim,
        "condicoes": subtitulo,
        "restricao": None,
        "minimo": minimo,
        "limite": limite,
        "imagem": imagem,
    }


def ler_cupons_ml(categorias: str = ML_CATEGORIAS_PADRAO) -> list[dict]:
    pote = _cookies_ml()
    if pote is None:
        raise SessaoMLExpirada("sem login salvo: rode o LOGIN-MERCADOLIVRE.bat")
    vistos: dict[str, dict] = {}
    sessao = requests.Session()
    sessao.cookies = pote
    for categoria in [c.strip() for c in categorias.split(",") if c.strip()]:
        resposta = sessao.get(ML_CUPONS.format(categoria=categoria), headers=_headers(), timeout=30)
        if "/login" in resposta.url or "_n.ctx.r=" not in resposta.text and "iniciar sessão" in resposta.text.lower():
            raise SessaoMLExpirada("o Mercado Livre pediu login de novo: rode o LOGIN-MERCADOLIVRE.bat")
        contexto = _contexto_ml(resposta.text)
        for obj in _objetos_cupom(contexto):
            cupom = interpretar_cupom_ml(obj)
            if cupom and cupom["codigo"] not in vistos:
                vistos[cupom["codigo"]] = cupom
        time.sleep(1.0)
    sessao.close()
    return list(vistos.values())


def atualizar_ml(con, agora: datetime, categorias: str = ML_CATEGORIAS_PADRAO) -> dict:
    preparar_tabelas(con)
    cupons = ler_cupons_ml(categorias)
    con.execute("DELETE FROM cupons WHERE loja = 'Mercado Livre'")
    for c in cupons:
        _gravar_cupom(con, c, agora)
    con.commit()
    ativos = [c for c in cupons if ativo(c, agora)]
    return {"cupons_ativos": len(ativos), "titulos": [c["titulo"] for c in ativos]}


# ---------------------------------------------------------------------------
# Postagem de cupom (anúncio do cupom em si, com link afiliado da loja)
# ---------------------------------------------------------------------------
def brl(valor: float) -> str:
    texto = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {texto[:-3] if texto.endswith(',00') else texto}"


def _forca(c: dict) -> float:
    """Ordem de preferência: porcentagem alta, ou valor alto em relação ao mínimo."""
    if c["tipo"] == "frete" or not c.get("valor"):
        return 5.0
    if c["tipo"] == "pct":
        return float(c["valor"])
    referencia = c.get("minimo") or c["valor"] * 10
    return 100.0 * c["valor"] / referencia if referencia else 0.0


def proximo_aviso(con, agora: datetime, intervalo_min: float, max_por_dia: int) -> dict | None:
    """O melhor cupom ainda não divulgado, respeitando o ritmo de avisos."""
    preparar_tabelas(con)
    ultimo = con.execute("SELECT loja, postado_em FROM cupons_postados ORDER BY postado_em DESC LIMIT 1").fetchone()
    if ultimo and ultimo[1] > _iso(agora - timedelta(minutes=intervalo_min)):
        return None
    hoje = con.execute("SELECT COUNT(*) FROM cupons_postados WHERE postado_em >= ?",
                       (agora.strftime("%Y-%m-%d 00:00:00"),)).fetchone()[0]
    if hoje >= max_por_dia:
        return None
    ja = {(l[0], l[1]) for l in con.execute("SELECT loja, codigo FROM cupons_postados")}
    linhas = con.execute(
        "SELECT loja, codigo, tipo, valor, descricao, link, inicio, fim, condicoes, restricao, minimo, limite, imagem, titulo, "
        "fonte, id_site, como, sem_codigo FROM cupons WHERE restricao IS NULL AND tipo IS NOT NULL"
    ).fetchall()
    nomes = ["loja", "codigo", "tipo", "valor", "descricao", "link", "inicio", "fim", "condicoes", "restricao",
             "minimo", "limite", "imagem", "titulo", "fonte", "id_site", "como", "sem_codigo"]
    opcoes = []
    for linha in linhas:
        c = dict(zip(nomes, linha))
        if chave_do_aviso(c) in ja or not ativo(c, agora):
            continue
        if c["fim"] and c["fim"] < _iso(agora + timedelta(hours=1)):
            continue  # acaba em menos de 1 h: não dá tempo de ninguém usar
        if c["loja"] == "KaBuM!" and not c["link"].startswith("http"):
            continue
        opcoes.append(c)
    for m in listar_manuais(con, agora):
        if m["situacao"] != "ativo" or not m["divulgar"]:
            continue
        c = manual_como_cupom(m)
        if chave_do_aviso(c) in ja:
            continue
        if c["fim"] and c["fim"] < _iso(agora + timedelta(hours=1)):
            continue
        opcoes.append(c)
    if not opcoes:
        return None
    # Cupom cadastrado por você (portal ou painel do site) vem primeiro. Depois
    # alterna as lojas: a mesma loja só repete se a outra não tiver cupom novo.
    loja_anterior = ultimo[0] if ultimo else None
    opcoes.sort(key=lambda c: (c.get("fonte") not in ("site", "manual"), c["loja"] == loja_anterior, -_forca(c)))
    return opcoes[0]


def chave_do_aviso(c: dict) -> tuple[str, str]:
    """Identidade do aviso em cupons_postados: cupom do painel vale pelo id (o código pode se repetir)."""
    if c.get("fonte") == "site" and c.get("id_site"):
        return c["loja"], f"site:{c['id_site']}"
    if c.get("fonte") == "manual" and c.get("id_manual"):
        return c["loja"], f"manual:{c['id_manual']}"
    return c["loja"], c["codigo"]


def sincronizar_do_site(con, agora: datetime) -> dict | None:
    """
    Busca no site os cupons cadastrados no painel que ainda não foram
    divulgados e troca a cópia local por eles. Sem site configurado: None.
    """
    import enviar_site

    if not enviar_site.configurado():
        return None
    resposta = requests.get(enviar_site._url() + "/api/integracao/cupons", headers=enviar_site._cabecalhos(),
                            timeout=20)
    resposta.raise_for_status()
    lista = resposta.json().get("cupons") or []
    preparar_tabelas(con)
    con.execute("DELETE FROM cupons WHERE fonte = 'site'")
    for c in lista:
        if not c.get("codigo") or not c.get("link") or not c.get("tipo"):
            continue
        con.execute(
            "INSERT OR REPLACE INTO cupons (loja, codigo, tipo, valor, descricao, link, inicio, fim, condicoes, "
            "restricao, minimo, atualizado_em, limite, imagem, titulo, fonte, id_site, como, sem_codigo) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, NULL, ?, 'site', ?, ?, ?)",
            (c["loja"], c["codigo"], c["tipo"], c.get("valor"), c.get("descricao"), c["link"], c.get("inicio"),
             c.get("fim"), c.get("minimo") or 0.0, _iso(agora), c.get("limite"), c.get("titulo"), c.get("id_site"),
             c.get("como"), 1 if c.get("sem_codigo") else 0),
        )
    con.commit()
    return {"cupons": len(lista), "lojas": sorted({c["loja"] for c in lista})}


def montar_aviso(c: dict) -> dict:
    """Pedido para o /api/post-deal: legenda pronta; o bot troca o link pelo afiliado."""
    desconto = texto_desconto(c["tipo"], c["valor"])
    minimo = c.get("minimo")
    linha_minimo = f"🛒 <b>Compra mínima:</b> {brl(minimo)}" if minimo else "🛒 <b>Compra mínima:</b> sem valor mínimo"
    validade = validade_legivel(c["fim"])
    link = c["link"]
    # Central de cupons do ML (lida com login) tem texto próprio; o cupom
    # manual do ML segue o modelo geral, com código ou "sem código".
    central_ml = c["loja"] == "Mercado Livre" and c.get("fonte") != "manual"
    if central_ml:
        linhas = [
            "🎟️ <b>CUPOM MERCADO LIVRE!</b>",
            "",
            f"🏷️ <b>Cupom:</b> {c.get('titulo') or desconto}",
            f"💸 <b>{desconto}</b> {('em ' + c['condicoes'][3:]) if (c.get('condicoes') or '').lower().startswith('em ') else ''}".rstrip(),
            linha_minimo,
        ]
        if c.get("limite"):
            linhas.append(f"📌 <b>Desconto máximo:</b> {brl(c['limite'])}")
        if validade:
            linhas.append(f"⏳ <b>Válido até:</b> {validade}")
        linhas += [
            "",
            "✅ <b>Como usar:</b> entre na sua conta do Mercado Livre, abra Cupons e toque em "
            "<b>Aplicar</b> (aparece para quem estiver elegível).",
            f"👉 <b>Pegar o cupom:</b> <a href=\"{link}\">Clique aqui</a>",
        ]
        titulo = f"Cupom Mercado Livre {c.get('titulo') or desconto}"
    else:
        do_site = c.get("fonte") in ("site", "manual")
        sem_codigo = bool(c.get("sem_codigo"))
        # Cupom do painel: o título é o resumo ("10% OFF em eletrônicos"); da KaBuM!, a descrição.
        descricao = (c.get("titulo") if do_site else c.get("descricao")) or desconto
        detalhe = re.sub(r"^\s*(?:R\$\s*[\d.,]+|\d+(?:[.,]\d+)?\s*%)\s*OFF\s*", "", descricao, flags=re.I).strip()
        if detalhe.casefold() == desconto.casefold():
            detalhe = ""  # "Frete grátis" repetiria o "FRETE GRÁTIS" do desconto
        if do_site:
            detalhe = html.escape(detalhe, quote=False)
        linhas = [f"🎟️ <b>CUPOM {html.escape(c['loja'].upper(), quote=False)} LIBERADO!</b>", ""]
        if sem_codigo:
            como = html.escape(c.get("como") or "ative o cupom na página da loja", quote=False)
            linhas.append(f"🏷️ <b>Sem código:</b> {como}")
        else:
            linhas.append(f"🏷️ <b>Código:</b> <code>{html.escape(c['codigo'], quote=False)}</code>")
        linhas += [f"💸 <b>{desconto}</b> {detalhe}".rstrip(), linha_minimo]
        if c.get("limite"):
            linhas.append(f"📌 <b>Desconto máximo:</b> {brl(c['limite'])}")
        if validade:
            linhas.append(f"⏳ <b>Válido até:</b> {validade}")
        if do_site and c.get("descricao"):
            linhas.append(f"ℹ️ {html.escape(c['descricao'], quote=False)}")
        chamada = "Pegar o cupom" if sem_codigo else "Ver produtos com o cupom"
        linhas += ["", f"👉 <b>{chamada}:</b> <a href=\"{link}\">Clique aqui</a>"]
        titulo = f"Cupom {c['loja']} {desconto}" if sem_codigo else f"Cupom {c['codigo']} {c['loja']}"
    # Dados da arte (o bot desenha a imagem com a logo do grupo e a da loja).
    if central_ml:
        detalhe_arte = (c.get("titulo") or "").replace(desconto, "").strip(" ·-") or "em produtos selecionados"
        if not detalhe_arte.lower().startswith("em "):
            detalhe_arte = f"{detalhe_arte} · produtos selecionados".strip(" ·")
    else:
        detalhe_arte = re.sub(r"^\s*(?:R\$\s*[\d.,]+|\d+(?:[.,]\d+)?\s*%)\s*OFF\s*", "",
                              c.get("descricao") or "", flags=re.I).strip()
    arte = {
        "loja": c["loja"],
        "desconto": desconto,
        "detalhe": detalhe_arte,
        "codigo": None if central_ml or c.get("sem_codigo") else c["codigo"],
        "semCodigo": ("APLIQUE EM CUPONS NA SUA CONTA" if central_ml
                      else "SEM CÓDIGO" if c.get("sem_codigo") else None),
        "minimo": brl(minimo) if minimo else "SEM MÍNIMO",
        "limite": brl(c["limite"]) if c.get("limite") else None,
        "validade": validade,
        "imagemProduto": c.get("imagem"),
    }
    return {
        "titulo": titulo,
        "link": link,
        "plataforma": c["loja"],
        "imagem_url": c.get("imagem"),
        "arte_cupom": arte,
        "caption": "\n".join(linhas),
        # Anúncio de cupom não é produto: o espelho do bot manda estes dados para
        # a página de cupons do site (não para o catálogo de produtos).
        "cupom_site": dados_para_o_site(c, desconto),
    }


def dados_para_o_site(c: dict, desconto: str) -> dict:
    """O cupom como o site (Tech Lobo > Cupons) guarda. Datas no horário de Brasília."""
    ml = c["loja"] == "Mercado Livre" and c.get("fonte") != "manual"
    return {
        # Cupom que veio do painel: o site só marca como divulgado.
        "id_site": c.get("id_site") if c.get("fonte") == "site" else None,
        "plataforma": c["loja"],
        "codigo": c["codigo"],
        "sem_codigo": ml or bool(c.get("sem_codigo")),
        "tipo": c["tipo"],
        "valor": c["valor"],
        "minimo": c.get("minimo") or 0,
        "limite": c.get("limite"),
        "inicio": c.get("inicio"),
        "fim": c.get("fim"),
        "titulo": (c.get("titulo") if ml else c.get("descricao")) or f"{desconto} na {c['loja']}",
        "descricao": c.get("condicoes"),
        "como": c.get("como") or (("Entre na sua conta do Mercado Livre, abra Cupons e toque em Aplicar "
                                   "(aparece para quem estiver elegível).") if ml else None),
    }


def registrar_aviso(con, c: dict, canais: str, agora: datetime) -> None:
    loja, codigo = chave_do_aviso(c)
    con.execute("INSERT INTO cupons_postados (loja, codigo, postado_em, canais) VALUES (?, ?, ?, ?)",
                (loja, codigo, _iso(agora), canais))
    con.commit()


# ---------------------------------------------------------------------------
# Cupons manuais (aba "Cupons" do portal)
# ---------------------------------------------------------------------------
LOJAS_MANUAIS = {
    "Amazon": "https://www.amazon.com.br/",
    "Mercado Livre": "https://www.mercadolivre.com.br/cupons",
    "KaBuM!": "https://www.kabum.com.br/hotsite/cupons",
    "AliExpress": "https://pt.aliexpress.com/",
    "Shopee": "https://shopee.com.br/",
    "Magalu": "https://www.magazineluiza.com.br/",
}
# Prefixo da chave do produto (piloto_automatico.identificar) -> loja.
LOJA_DA_CHAVE = {"amazon": "Amazon", "ml": "Mercado Livre", "kabum": "KaBuM!", "ali": "AliExpress",
                 "shopee": "Shopee"}
CAMPOS_MANUAIS = ["id", "loja", "codigo", "sem_codigo", "como", "tipo", "valor", "minimo", "limite", "inicio",
                  "fim", "link", "titulo", "descricao", "aplicar_ofertas", "palavras", "divulgar", "criado_em",
                  "atualizado_em"]


class CupomInvalido(ValueError):
    pass


def _data_form(texto) -> str | None:
    """'2026-10-15T23:59' (campo datetime-local) ou '2026-10-15' -> 'AAAA-MM-DD HH:MM:SS'."""
    texto = str(texto or "").strip()
    if not texto:
        return None
    for formato in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return _iso(datetime.strptime(texto, formato))
        except ValueError:
            pass
    try:
        return _iso(datetime.strptime(texto, "%Y-%m-%d").replace(hour=23, minute=59, second=59))
    except ValueError:
        raise CupomInvalido(f"data inválida: {texto}")


def _numero(valor) -> float | None:
    if valor in (None, ""):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    numero = _num_br(str(valor).replace("R$", "").replace("%", "").strip())
    if numero is None:
        raise CupomInvalido(f"número inválido: {valor}")
    return numero


def salvar_manual(con, dados: dict, agora: datetime) -> int:
    """Cria (sem id) ou atualiza (com id) um cupom manual. Devolve o id."""
    preparar_tabelas(con)
    loja = str(dados.get("loja") or "").strip()
    if loja not in LOJAS_MANUAIS:
        raise CupomInvalido("escolha a loja")
    sem_codigo = bool(dados.get("sem_codigo"))
    codigo = re.sub(r"\s+", "", str(dados.get("codigo") or "")).upper()
    if not sem_codigo and not codigo:
        raise CupomInvalido("informe o código (ou marque \"sem código\")")
    tipo = str(dados.get("tipo") or "").strip()
    if tipo not in ("pct", "valor", "frete"):
        raise CupomInvalido("escolha o tipo de desconto")
    valor = _numero(dados.get("valor"))
    if tipo != "frete" and not valor:
        raise CupomInvalido("informe o valor do desconto")
    if tipo == "pct" and not 0 < valor < 100:
        raise CupomInvalido("porcentagem deve ficar entre 1 e 99")
    inicio, fim = _data_form(dados.get("inicio")), _data_form(dados.get("fim"))
    if fim and inicio and fim <= inicio:
        raise CupomInvalido("a validade termina antes de começar")
    link = str(dados.get("link") or "").strip() or LOJAS_MANUAIS[loja]
    if not link.startswith("http"):
        raise CupomInvalido("o link precisa começar com http")
    palavras = ", ".join(p.strip() for p in str(dados.get("palavras") or "").split(",") if p.strip())
    valores = {
        "loja": loja, "codigo": None if sem_codigo else codigo, "sem_codigo": int(sem_codigo),
        "como": (str(dados.get("como") or "").strip() or None) if sem_codigo else None,
        "tipo": tipo, "valor": valor if tipo != "frete" else None,
        "minimo": _numero(dados.get("minimo")) or 0.0, "limite": _numero(dados.get("limite")),
        "inicio": inicio, "fim": fim, "link": link,
        "titulo": str(dados.get("titulo") or "").strip()[:120] or None,
        "descricao": str(dados.get("descricao") or "").strip()[:300] or None,
        "aplicar_ofertas": int(bool(dados.get("aplicar_ofertas"))), "palavras": palavras or None,
        "divulgar": int(bool(dados.get("divulgar", True))), "atualizado_em": _iso(agora),
    }
    ident = dados.get("id")
    if ident:
        colunas = ", ".join(f"{k} = ?" for k in valores)
        cur = con.execute(f"UPDATE cupons_manuais SET {colunas} WHERE id = ?", (*valores.values(), int(ident)))
        if not cur.rowcount:
            raise CupomInvalido("cupom não encontrado")
        con.commit()
        return int(ident)
    valores["criado_em"] = _iso(agora)
    cur = con.execute(
        f"INSERT INTO cupons_manuais ({', '.join(valores)}) VALUES ({', '.join('?' for _ in valores)})",
        tuple(valores.values()),
    )
    con.commit()
    return int(cur.lastrowid)


def remover_manual(con, ident: int) -> bool:
    preparar_tabelas(con)
    cur = con.execute("DELETE FROM cupons_manuais WHERE id = ?", (int(ident),))
    con.commit()
    return bool(cur.rowcount)


def listar_manuais(con, agora: datetime) -> list[dict]:
    preparar_tabelas(con)
    linhas = con.execute(f"SELECT {', '.join(CAMPOS_MANUAIS)} FROM cupons_manuais ORDER BY id DESC").fetchall()
    postados = {}
    for codigo, quando, canais in con.execute(
        "SELECT codigo, MAX(postado_em), canais FROM cupons_postados WHERE codigo LIKE 'manual:%' GROUP BY codigo"
    ):
        postados[codigo] = (quando, canais)
    saida = []
    agora_txt = _iso(agora)
    for linha in linhas:
        m = dict(zip(CAMPOS_MANUAIS, linha))
        if m["fim"] and m["fim"] <= agora_txt:
            m["situacao"] = "vencido"
        elif m["inicio"] and m["inicio"] > agora_txt:
            m["situacao"] = "agendado"
        else:
            m["situacao"] = "ativo"
        m["desconto_txt"] = texto_desconto(m["tipo"], m["valor"] or 0)
        m["validade_txt"] = validade_legivel(m["fim"])
        divulgado = postados.get(f"manual:{m['id']}")
        m["divulgado_em"], m["divulgado_canais"] = divulgado if divulgado else (None, None)
        saida.append(m)
    return saida


def manual_por_id(con, ident: int, agora: datetime) -> dict | None:
    return next((m for m in listar_manuais(con, agora) if m["id"] == int(ident)), None)


def manual_como_cupom(m: dict) -> dict:
    """No formato das linhas da tabela cupons (o que proximo_aviso e montar_aviso esperam)."""
    return {
        "loja": m["loja"], "codigo": m["codigo"] or f"MANUAL{m['id']}", "tipo": m["tipo"],
        "valor": m["valor"] or 0, "descricao": m.get("descricao"), "link": m["link"], "inicio": m["inicio"],
        "fim": m["fim"], "condicoes": None, "restricao": None, "minimo": m["minimo"] or 0.0,
        "limite": m["limite"], "imagem": None, "titulo": m.get("titulo"), "fonte": "manual", "id_site": None,
        "id_manual": m["id"], "como": m.get("como"), "sem_codigo": m["sem_codigo"],
    }


def _titulo_tem(titulo: str, palavras: str | None) -> bool:
    if not palavras:
        return True
    import unicodedata

    def norm(t):
        return unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()

    alvo = norm(titulo)
    return any(norm(p.strip()) in alvo for p in palavras.split(",") if p.strip())


def cupom_manual_para_oferta(con, chave: str, item: dict, preco: float, agora: datetime) -> dict | None:
    """Melhor cupom manual marcado "aplicar nas ofertas" que vale para este produto."""
    loja = LOJA_DA_CHAVE.get((chave or "").split(":", 1)[0])
    if not loja:
        return None
    preparar_tabelas(con)
    agora_txt = _iso(agora)
    melhor = None
    for linha in con.execute(
        f"SELECT {', '.join(CAMPOS_MANUAIS)} FROM cupons_manuais WHERE loja = ? AND aplicar_ofertas = 1", (loja,)
    ):
        m = dict(zip(CAMPOS_MANUAIS, linha))
        if (m["fim"] and m["fim"] <= agora_txt) or (m["inicio"] and m["inicio"] > agora_txt):
            continue
        if m["minimo"] and preco < m["minimo"]:
            continue
        if not _titulo_tem(item.get("titulo") or "", m["palavras"]):
            continue
        final = None
        if m["tipo"] in ("pct", "valor"):
            final = aplicar(preco, m["tipo"], m["valor"] or 0)
            if m["tipo"] == "pct" and m["limite"]:
                final = max(final, round(preco - m["limite"], 2))
            if final <= 0 or final >= preco:
                continue
        cupom = {
            "codigo": None if m["sem_codigo"] else m["codigo"],
            "desconto": texto_desconto(m["tipo"], m["valor"] or 0),
            "preco_com_cupom": final,
            "validade": validade_legivel(m["fim"]),
            "fim": m["fim"],
            "origem": "manual",
            "como": (m["como"] or "ative o cupom na página da loja") if m["sem_codigo"] else None,
            "minimo": m["minimo"] or 0.0,
        }
        if not melhor or (final or float("inf")) < (melhor["preco_com_cupom"] or float("inf")):
            melhor = cupom
    return melhor
