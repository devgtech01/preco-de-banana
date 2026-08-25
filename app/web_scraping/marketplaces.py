"""
Registro único das lojas suportadas pela busca.

Antes o portal (app.py) e a linha de comando (main.py) tinham cada um a sua
cadeia de `if platform == ...`, com Mercado Livre e Amazon escritos na mão nos
dois lugares. Com cinco marketplaces isso vira erro garantido: alguém adiciona
uma loja num arquivo e esquece do outro. Aqui fica a lista canônica — para
somar uma loja nova basta implementar o scraper e registrar uma linha abaixo.

O rótulo (`nome`) é o mesmo que o scraper grava no campo `plataforma` de cada
produto, porque é por ele que o bot de divulgação decide qual link de afiliado
aplicar.
"""

from aliexpress_scraper import AliExpressScraper
from amazon_scraper import AmazonScraper
from kabum_scraper import KabumScraper
from scraper import MercadoLivreScraper
from shopee_scraper import ShopeeScraper

MARKETPLACES: dict[str, dict] = {
    "mercadolivre": {"classe": MercadoLivreScraper, "nome": "Mercado Livre", "emoji": "🛒"},
    "amazon": {"classe": AmazonScraper, "nome": "Amazon Brasil", "emoji": "📦"},
    "shopee": {"classe": ShopeeScraper, "nome": "Shopee", "emoji": "🧡"},
    "aliexpress": {"classe": AliExpressScraper, "nome": "AliExpress", "emoji": "🌐"},
    "kabum": {"classe": KabumScraper, "nome": "KaBuM!", "emoji": "💙"},
}

CHAVES = list(MARKETPLACES)

# "both" existia antes das lojas novas e significava Mercado Livre + Amazon.
# Continua significando isso: buscas e atalhos salvos pelo usuário não podem
# mudar de comportamento porque o programa cresceu.
PADRAO_AMBOS = ["mercadolivre", "amazon"]

APELIDOS = {
    "ml": "mercadolivre",
    "mercado livre": "mercadolivre",
    "mercadolibre": "mercadolivre",
    "meli": "mercadolivre",
    "amazon brasil": "amazon",
    "amz": "amazon",
    "ali": "aliexpress",
    "ali express": "aliexpress",
    "kabum!": "kabum",
    "ka bum": "kabum",
}

TODOS = ("all", "todos", "todas", "tudo")
AMBOS = ("both", "ambos", "ambas")


def resolver_plataformas(valor) -> list[str]:
    """
    Traduz o que veio da interface ou da linha de comando em chaves válidas.

    Aceita uma chave só ("kabum"), várias separadas por vírgula
    ("shopee,kabum"), uma lista, "both" (ML + Amazon) ou "all" (tudo).
    Valor desconhecido cai no padrão histórico em vez de devolver busca vazia.
    """
    if isinstance(valor, (list, tuple, set)):
        pedidos = [str(item) for item in valor]
    else:
        pedidos = str(valor or "").split(",")

    chaves: list[str] = []
    for pedido in pedidos:
        bruto = pedido.strip().lower()
        if not bruto:
            continue

        if bruto in TODOS:
            chaves.extend(CHAVES)
            continue
        if bruto in AMBOS:
            chaves.extend(PADRAO_AMBOS)
            continue

        chave = APELIDOS.get(bruto, bruto)
        if chave in MARKETPLACES:
            chaves.append(chave)

    # dict.fromkeys remove repetição preservando a ordem pedida.
    unicas = list(dict.fromkeys(chaves))
    return unicas or list(PADRAO_AMBOS)


def criar_scraper(chave: str, headless: bool = True):
    """Instancia o scraper da loja. Levanta KeyError para chave inválida."""
    return MARKETPLACES[chave]["classe"](headless=headless)


def rotulo(chave: str) -> str:
    info = MARKETPLACES.get(chave)
    return info["nome"] if info else chave


def descricao(chaves: list[str]) -> str:
    """Texto pronto para log/UI: '🛒 Mercado Livre + 💙 KaBuM!'."""
    partes = []
    for chave in chaves:
        info = MARKETPLACES.get(chave)
        partes.append(f"{info['emoji']} {info['nome']}" if info else chave)
    return " + ".join(partes)


def cota_por_loja(limite: int, quantidade_de_lojas: int) -> int:
    """
    Quantos itens pedir para cada loja quando a busca cobre mais de uma.

    O piso de 4 evita que um limite baixo dividido por cinco lojas peça
    praticamente nada de cada uma.
    """
    if quantidade_de_lojas <= 1:
        return limite
    return max(4, limite // quantidade_de_lojas)


def intercalar(grupos: list[list[dict]]) -> list[dict]:
    """
    Mistura os resultados alternando entre as lojas.

    Sem isso a tabela do portal mostra todos os produtos de uma loja e só depois
    os da outra — quem procura o menor preço precisa rolar até o fim para
    comparar. Alternando, as primeiras linhas já trazem uma amostra de cada.
    """
    combinados: list[dict] = []
    maior = max((len(grupo) for grupo in grupos), default=0)

    for indice in range(maior):
        for grupo in grupos:
            if indice < len(grupo):
                combinados.append(grupo[indice])

    return combinados
