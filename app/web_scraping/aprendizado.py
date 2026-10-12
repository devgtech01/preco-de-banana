"""
Aprendizado do piloto: o que é preço normal e o quanto cada loja exagera.

Tudo sai do histórico que o piloto já grava (data/piloto.db, tabela precos)
e melhora sozinho conforme os dias passam. Nada aqui precisa de internet,
placa de vídeo ou biblioteca extra.

1. Preço normal de cada produto
   O histórico é resumido em um preço por dia (a mediana do dia), para que dez
   leituras na mesma tarde não pesem como dez dias. Com pelo menos MIN_DIAS
   dias anteriores a hoje, o produto passa a ter:
     - preço normal: mediana dos dias dos últimos 30 dias (ou de tudo o que
       houver, até 90 dias);
     - menor preço dos últimos 90 dias.

2. Maquiagem de preço ("sobe antes, dá desconto depois")
   Se nos últimos JANELA_RECENTE_DIAS dias o preço subiu ALTA_SUSPEITA acima do
   patamar que tinha antes, o preço normal passa a ser o patamar ANTERIOR à
   alta. Um "50% OFF" que só devolve o preço ao que era não passa. É o truque
   mais comum na Black Friday.

3. Confiança no "de/por" de cada loja (aprendida)
   Para produtos com histórico, compara o desconto que a loja anuncia com a
   queda real em relação ao preço normal. A razão (soma das quedas reais /
   soma dos descontos anunciados, últimos 30 dias) é a confiança da loja.
   Produto sem histórico próprio usa essa confiança para estimar o desconto
   real a partir do anunciado. Até juntar MIN_AMOSTRAS amostras de uma loja,
   vale o valor fixo de antes (AliExpress 0.6, demais 1.0).

4. Catálogo de produtos (tabela produtos)
   Título, loja, termo, categoria e quando foi visto. É a matéria-prima da
   próxima etapa (estimar o preço justo de um produto nunca visto pelo título).
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta

MIN_DIAS = 3                 # dias anteriores a hoje para o histórico valer
JANELA_DIAS = 90
JANELA_NORMAL_DIAS = 30
JANELA_RECENTE_DIAS = 14
ALTA_SUSPEITA = 1.10         # subiu 10% acima do patamar anterior = suspeito
MIN_AMOSTRAS = 30            # amostras por loja para a confiança aprendida valer
CONFIANCA_PISO = 0.3
CONFIANCA_PADRAO = {"ali": 0.6}
RECALCULAR_HORAS = 6
DESCONTO_FALSO = 0.05        # anunciou 15%+ e o preço real caiu menos que isso

LOJAS = {"ml": "Mercado Livre", "amazon": "Amazon", "kabum": "KaBuM!", "ali": "AliExpress", "shopee": "Shopee"}


def _iso(momento: datetime) -> str:
    return momento.strftime("%Y-%m-%d %H:%M:%S")


def loja_da_chave(chave: str) -> str:
    return (chave or "").split(":", 1)[0]


# ---------------------------------------------------------------------------
# Banco
# ---------------------------------------------------------------------------
def preparar_tabelas(con) -> None:
    colunas = {linha[1] for linha in con.execute("PRAGMA table_info(precos)")}
    if "de" not in colunas:
        con.execute("ALTER TABLE precos ADD COLUMN de REAL")   # preço "de" anunciado naquela leitura
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS produtos (
            chave TEXT PRIMARY KEY,
            loja TEXT,
            titulo TEXT,
            termo TEXT,
            categoria TEXT,
            link TEXT,
            primeiro_visto TEXT,
            ultimo_visto TEXT,
            vezes INTEGER DEFAULT 0
        );
        """
    )
    con.commit()


def registrar_produto(con, chave: str, item: dict, link: str, termo: str, categoria: str, agora: datetime) -> None:
    quando = _iso(agora)
    con.execute(
        """
        INSERT INTO produtos (chave, loja, titulo, termo, categoria, link, primeiro_visto, ultimo_visto, vezes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(chave) DO UPDATE SET
            titulo = excluded.titulo, termo = excluded.termo, categoria = excluded.categoria,
            link = excluded.link, ultimo_visto = excluded.ultimo_visto, vezes = produtos.vezes + 1
        """,
        (chave, str(item.get("plataforma") or ""), str(item.get("titulo") or "")[:300], termo, categoria,
         link, quando, quando),
    )


# ---------------------------------------------------------------------------
# Preço normal de um produto
# ---------------------------------------------------------------------------
def leituras(con, chave: str, agora: datetime) -> list[tuple[str, float]]:
    """(visto_em, preço) dos últimos JANELA_DIAS dias, antes de agora."""
    inicio = _iso(agora - timedelta(days=JANELA_DIAS))
    return [
        (v, p) for v, p in con.execute(
            "SELECT visto_em, preco FROM precos WHERE chave = ? AND visto_em >= ? AND visto_em < ? ORDER BY visto_em",
            (chave, inicio, _iso(agora)),
        )
        if p and p > 0
    ]


def por_dia(linhas: list[tuple[str, float]], antes_do_dia: str) -> list[tuple[str, float]]:
    """Um preço por dia (mediana do dia), só dos dias anteriores a `antes_do_dia` (AAAA-MM-DD)."""
    dias: dict[str, list[float]] = {}
    for visto_em, preco in linhas:
        dia = visto_em[:10]
        if dia < antes_do_dia:
            dias.setdefault(dia, []).append(preco)
    return [(dia, statistics.median(v)) for dia, v in sorted(dias.items())]


def referencia(dias: list[tuple[str, float]], hoje: str) -> dict | None:
    """
    Preço normal, menor preço e se houve alta suspeita recente. None se ainda
    não há MIN_DIAS dias de histórico.
    """
    if len(dias) < MIN_DIAS:
        return None
    hoje_dt = datetime.strptime(hoje, "%Y-%m-%d")
    corte_normal = (hoje_dt - timedelta(days=JANELA_NORMAL_DIAS)).strftime("%Y-%m-%d")
    corte_recente = (hoje_dt - timedelta(days=JANELA_RECENTE_DIAS)).strftime("%Y-%m-%d")

    todos = [p for _, p in dias]
    ultimos = [p for d, p in dias if d >= corte_normal]
    normal = statistics.median(ultimos if len(ultimos) >= MIN_DIAS else todos)

    maquiado = False
    antigos = [p for d, p in dias if d < corte_recente]
    recentes = [p for d, p in dias if d >= corte_recente]
    if len(antigos) >= MIN_DIAS and recentes:
        patamar = statistics.median(antigos)
        if max(recentes) >= patamar * ALTA_SUSPEITA and patamar < normal:
            normal = patamar
            maquiado = True

    return {"normal": normal, "minimo": min(todos), "dias": len(dias), "maquiado": maquiado}


def referencia_do_produto(con, chave: str, agora: datetime) -> tuple[dict | None, list[float]]:
    """
    (referência por dias, preços brutos). Os brutos servem à regra antiga
    (3 leituras) enquanto o produto não completa MIN_DIAS dias.
    """
    linhas = leituras(con, chave, agora)
    hoje = agora.strftime("%Y-%m-%d")
    bruto_30 = _iso(agora - timedelta(days=30))
    brutos = [p for v, p in linhas if v >= bruto_30]
    return referencia(por_dia(linhas, hoje), hoje), brutos


# ---------------------------------------------------------------------------
# Confiança no "de/por" de cada loja
# ---------------------------------------------------------------------------
def recalcular_confianca(con, agora: datetime) -> dict:
    """
    Para cada produto com histórico e com preço "de" anunciado nos últimos 30
    dias: desconto anunciado x queda real em relação ao preço normal daquele
    dia. Agrega por loja e grava em estado['aprendizado'].
    """
    inicio = _iso(agora - timedelta(days=JANELA_DIAS))
    recente = (agora - timedelta(days=JANELA_NORMAL_DIAS)).strftime("%Y-%m-%d")
    por_chave: dict[str, list[tuple[str, float, float | None]]] = {}
    for chave, visto_em, preco, de in con.execute(
        "SELECT chave, visto_em, preco, de FROM precos WHERE visto_em >= ? ORDER BY chave, visto_em", (inicio,)
    ):
        if preco and preco > 0:
            por_chave.setdefault(chave, []).append((visto_em, preco, de))

    soma: dict[str, dict] = {}
    com_historico = 0
    hoje = agora.strftime("%Y-%m-%d")
    for chave, linhas in por_chave.items():
        dias_distintos = {v[:10] for v, _, _ in linhas}
        if len([d for d in dias_distintos if d < hoje]) >= MIN_DIAS:
            com_historico += 1
        # Último dia (recente) em que a loja anunciou "de" acima do preço.
        anunciados = [(v, p, de) for v, p, de in linhas if de and de > p and v[:10] >= recente]
        if not anunciados:
            continue
        dia = anunciados[-1][0][:10]
        ref = referencia(por_dia([(v, p) for v, p, _ in linhas], dia), dia)
        if not ref or ref["normal"] <= 0:
            continue
        do_dia = [(p, de) for v, p, de in anunciados if v[:10] == dia]
        preco = statistics.median(p for p, _ in do_dia)
        de = statistics.median(d for _, d in do_dia)
        anunciado = (de - preco) / de
        real = max(0.0, (ref["normal"] - preco) / ref["normal"])
        loja = loja_da_chave(chave)
        s = soma.setdefault(loja, {"anunciado": 0.0, "real": 0.0, "amostras": 0, "falsos": 0, "relevantes": 0})
        s["anunciado"] += anunciado
        s["real"] += min(real, anunciado)
        s["amostras"] += 1
        if anunciado >= 0.15:
            s["relevantes"] += 1
            if real < DESCONTO_FALSO:
                s["falsos"] += 1

    lojas = {}
    for loja, s in soma.items():
        conf = s["real"] / s["anunciado"] if s["anunciado"] > 0 else None
        lojas[loja] = {
            "nome": LOJAS.get(loja, loja),
            "amostras": s["amostras"],
            "confianca": round(max(CONFIANCA_PISO, min(1.0, conf)), 3) if conf is not None else None,
            "aprendida": s["amostras"] >= MIN_AMOSTRAS and conf is not None,
            "falsos_pct": round(100 * s["falsos"] / s["relevantes"]) if s["relevantes"] else None,
        }
    dados = {"atualizado_em": _iso(agora), "produtos_com_historico": com_historico,
             "produtos": len(por_chave), "lojas": lojas}
    con.execute(
        "INSERT INTO estado (chave, valor) VALUES ('aprendizado', ?) "
        "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
        (json.dumps(dados, ensure_ascii=False),),
    )
    con.commit()
    return dados


def carregar(con) -> dict:
    linha = con.execute("SELECT valor FROM estado WHERE chave = 'aprendizado'").fetchone()
    try:
        return json.loads(linha[0]) if linha else {}
    except (TypeError, ValueError):
        return {}


def recalcular_se_preciso(con, agora: datetime) -> dict | None:
    """Recalcula a cada RECALCULAR_HORAS. Devolve os dados novos ou None."""
    atual = carregar(con)
    if atual.get("atualizado_em", "") > _iso(agora - timedelta(hours=RECALCULAR_HORAS)):
        return None
    return recalcular_confianca(con, agora)


def confianca(dados: dict, chave: str) -> tuple[float, bool]:
    """(confiança no "de/por" da loja do produto, se é aprendida)."""
    loja = loja_da_chave(chave)
    info = (dados.get("lojas") or {}).get(loja) or {}
    if info.get("aprendida") and info.get("confianca") is not None:
        return float(info["confianca"]), True
    return CONFIANCA_PADRAO.get(loja, 1.0), False


def descrever(dados: dict) -> str:
    partes = []
    for loja, info in sorted((dados.get("lojas") or {}).items()):
        if info.get("confianca") is None:
            continue
        extra = [f"{info['amostras']} amostras"]
        if info.get("falsos_pct") is not None:
            extra.insert(0, f"{info['falsos_pct']}% dos descontos eram falsos")
        if not info.get("aprendida"):
            extra.append("ainda juntando")
        txt = f"{info['nome']} {round(100 * info['confianca'])}% ({', '.join(extra)})"
        partes.append(txt)
    return (f"{dados.get('produtos_com_historico', 0)} produtos com {MIN_DIAS}+ dias de histórico. "
            "Confiança no de/por: " + ("; ".join(partes) if partes else "ainda sem amostras") + ".")


# ---------------------------------------------------------------------------
# Radar: todos os produtos monitorados, para a aba "Radar" do portal
# ---------------------------------------------------------------------------
_cache_radar: dict = {"marca": None, "quando": 0.0, "itens": []}
RADAR_CACHE_S = 90


def _link_da_chave(chave: str) -> str | None:
    loja, _, ident = (chave or "").partition(":")
    if loja == "amazon":
        return f"https://www.amazon.com.br/dp/{ident}"
    if loja == "ali":
        return f"https://pt.aliexpress.com/item/{ident}.html"
    if loja == "kabum":
        return f"https://www.kabum.com.br/produto/{ident}"
    if loja == "ml" and ident.startswith("MLB"):
        return f"https://produto.mercadolivre.com.br/MLB-{ident[3:]}"
    if loja == "url":
        return ident
    return None


def _montar_radar(con, agora: datetime) -> list[dict]:
    inicio = _iso(agora - timedelta(days=JANELA_DIAS))
    hoje = agora.strftime("%Y-%m-%d")
    produtos = {
        c: {"loja": l, "titulo": t, "termo": te, "categoria": ca, "link": li}
        for c, l, t, te, ca, li in con.execute("SELECT chave, loja, titulo, termo, categoria, link FROM produtos")
    }
    postados: dict[str, tuple] = {}
    for chave, titulo, link, preco, quando in con.execute(
        "SELECT chave, titulo, link, preco, postado_em FROM postados ORDER BY postado_em"
    ):
        postados[chave] = (titulo, link, preco, quando)

    itens: list[dict] = []
    atual_chave, linhas, ultimo_termo, ultimo_ref = None, [], None, None

    def fechar():
        if not atual_chave or not linhas:
            return
        visto_em, preco, de = linhas[-1]
        ref = referencia(por_dia([(v, p) for v, p, _ in linhas], hoje), hoje)
        prod = produtos.get(atual_chave) or {}
        post = postados.get(atual_chave)
        variacao = None
        if ref and ref["normal"] > 0:
            variacao = round((preco - ref["normal"]) / ref["normal"], 4)
        itens.append({
            "chave": atual_chave,
            "loja": LOJAS.get(loja_da_chave(atual_chave), loja_da_chave(atual_chave)),
            "titulo": prod.get("titulo") or (post[0] if post else None),
            "link": prod.get("link") or (post[1] if post else None) or _link_da_chave(atual_chave),
            "categoria": prod.get("categoria"),
            "termo": prod.get("termo") or ultimo_termo,
            "preco": preco,
            "de": de,
            "normal": round(ref["normal"], 2) if ref else None,
            "minimo": round(ref["minimo"], 2) if ref else None,
            "dias": len({v[:10] for v, _, _ in linhas}),   # dias com leitura, hoje incluído
            "maquiado": bool(ref and ref["maquiado"]),
            "variacao": variacao,
            "leituras": len(linhas),
            "visto_em": visto_em,
            "postado_em": post[3] if post else None,
            "postado_preco": post[2] if post else None,
            # Última leitura bateu com a busca (não é acessório, usado...)? Antigo: sem a marca.
            "relevante": ultimo_ref != 0,
        })

    for chave, visto_em, preco, de, termo, ref in con.execute(
        "SELECT chave, visto_em, preco, de, termo, ref FROM precos WHERE visto_em >= ? ORDER BY chave, visto_em",
        (inicio,),
    ):
        if chave != atual_chave:
            fechar()
            atual_chave, linhas, ultimo_termo, ultimo_ref = chave, [], None, None
        if preco and preco > 0:
            linhas.append((visto_em, preco, de))
        if termo:
            ultimo_termo = termo
        if ref is not None:
            ultimo_ref = ref
    fechar()
    return itens


def radar(con, agora: datetime, busca: str = "", loja: str = "", filtro: str = "", ordem: str = "recentes",
          limite: int = 50, pagina: int = 1, irrelevantes: bool = False) -> dict:
    """Lista paginada dos produtos monitorados, com preço normal e variação."""
    import time as _time
    import unicodedata

    marca = con.execute("SELECT MAX(visto_em), COUNT(*) FROM precos").fetchone()
    if _cache_radar["marca"] != marca or _time.time() - _cache_radar["quando"] > RADAR_CACHE_S:
        _cache_radar.update(marca=marca, quando=_time.time(), itens=_montar_radar(con, agora))
    todos = _cache_radar["itens"]

    def norm(t):
        return unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()

    itens = todos if irrelevantes else [i for i in todos if i["relevante"] or i["postado_em"]]
    if loja:
        itens = [i for i in itens if loja_da_chave(i["chave"]) == loja]
    if busca:
        partes = norm(busca).split()
        itens = [i for i in itens
                 if all(p in norm(f"{i['titulo'] or ''} {i['termo'] or ''} {i['chave']}") for p in partes)]
    if filtro == "historico":
        itens = [i for i in itens if i["normal"] is not None]
    elif filtro == "queda":
        itens = [i for i in itens if i["variacao"] is not None and i["variacao"] <= -0.10]
    elif filtro == "alta":
        itens = [i for i in itens if i["variacao"] is not None and i["variacao"] >= 0.10]
    elif filtro == "maquiados":
        itens = [i for i in itens if i["maquiado"]]
    elif filtro == "postados":
        itens = [i for i in itens if i["postado_em"]]

    if ordem == "queda":
        itens = sorted(itens, key=lambda i: (i["variacao"] is None, i["variacao"] or 0))
    elif ordem == "preco":
        itens = sorted(itens, key=lambda i: i["preco"])
    elif ordem == "preco_desc":
        itens = sorted(itens, key=lambda i: -i["preco"])
    elif ordem == "vistos":
        itens = sorted(itens, key=lambda i: -i["leituras"])
    else:
        itens = sorted(itens, key=lambda i: i["visto_em"], reverse=True)

    limite = max(10, min(200, int(limite or 50)))
    pagina = max(1, int(pagina or 1))
    por_loja: dict[str, int] = {}
    for i in todos:
        por_loja[loja_da_chave(i["chave"])] = por_loja.get(loja_da_chave(i["chave"]), 0) + 1
    return {
        "total": len(itens),
        "pagina": pagina,
        "paginas": max(1, -(-len(itens) // limite)),
        "itens": itens[(pagina - 1) * limite: pagina * limite],
        "resumo": {
            "produtos": len(todos),
            "irrelevantes": sum(1 for i in todos if not i["relevante"] and not i["postado_em"]),
            "com_historico": sum(1 for i in todos if i["normal"] is not None),
            "em_queda": sum(1 for i in todos if i["variacao"] is not None and i["variacao"] <= -0.10),
            "maquiados": sum(1 for i in todos if i["maquiado"]),
            "por_loja": por_loja,
        },
    }
