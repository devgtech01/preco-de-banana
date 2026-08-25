"""
Scraper de busca da Shopee Brasil, em dois caminhos.

1. API DE AFILIADOS (preferido, ver shopee_api.py). Autentica por AppID+Secret e
   responde de qualquer rede — inclusive das institucionais e de datacenter, que
   a Shopee barra na entrada. Ainda devolve o link já afiliado.

2. RASPAGEM PELO NAVEGADOR (reserva, quando não há credencial). A vitrine é
   montada por JavaScript e a API interna do site responde 403 sem os cookies
   que ele mesmo emite, então a consulta é feita de dentro da própria página,
   pelo Firefox mascarado.

O caminho 2 depende inteiramente do IP de saída: de rede que não pareça
residencial a Shopee troca a busca pela tela /verify/traffic — e faz isso também
com a janela visível, ou seja, não é algo que raspar melhor resolva. Quando
acontece, o motivo vai para o portal em vez de virar um "0 produtos" mudo.
"""

import re
import sys
import urllib.parse

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from rich.console import Console

from base_scraper import MarketplaceScraper
from shopee_api import ShopeeAffiliateAPI
from utils import clean_price

console = Console(force_terminal=True)

# A API devolve valores inteiros multiplicados por 100.000 (R$ 12,90 => 1290000).
FATOR_PRECO = 100000.0

CONSULTA_API = """
async ([keyword, newest, limite]) => {
    const url = `/api/v4/search/search_items?by=relevancy&keyword=${encodeURIComponent(keyword)}`
        + `&limit=${limite}&newest=${newest}&order=desc&page_type=search`
        + `&scenario=PAGE_GLOBAL_SEARCH&version=2`;
    try {
        const resposta = await fetch(url, {
            headers: {
                'x-api-source': 'pc',
                'x-shopee-language': 'pt-BR',
                'x-requested-with': 'XMLHttpRequest'
            },
            credentials: 'include'
        });
        if (!resposta.ok) return { erro: 'HTTP ' + resposta.status };
        return await resposta.json();
    } catch (e) {
        return { erro: String(e) };
    }
}
"""


class ShopeeScraper(MarketplaceScraper):
    nome = "Shopee"
    perfil = "shopee"
    emoji = "🧡"
    suporta_http = False        # a API só responde com os cookies do site
    espera_seletor = "a[href*='-i.'], li[data-sqe='item'], .shopee-search-item-result__item"
    # Uma única tentativa: quando a Shopee manda para /verify/traffic ela está
    # barrando o IP, e esperar 10 segundos para tentar de novo só faz o usuário
    # olhar para a ampulheta — o bloqueio não expira nesse intervalo.
    limite_tentativas = 1
    # A janela visível NÃO entra como sugestão aqui de propósito: a Shopee
    # decide pelo IP antes de olhar o navegador, e o bloqueio se repete
    # idêntico com --headful. Sugerir isso só faria o usuário perder tempo.
    dica_bloqueio = (
        "Saída definitiva: cadastre o App ID e o App Secret da API de Afiliados "
        "na tela de configuração do portal (engrenagem > Shopee — API de "
        "Afiliados) — ela responde de qualquer rede e já traz o link afiliado. "
        "Alternativas: rodar de uma rede doméstica (empresa, escola e órgão "
        "público costumam ser barrados) ou configurar SCRAPER_PROXY no .env.local."
    )

    base_url = "https://shopee.com.br/search"
    itens_por_pagina = 60

    # ------------------------------------------------------------------
    # Caminho preferido: API oficial de afiliados
    # ------------------------------------------------------------------
    def search_products(self, query: str, max_pages: int = 1, max_items: int = 100) -> list[dict]:
        api = ShopeeAffiliateAPI()
        erro_da_api = None

        if api.configurada():
            console.print(
                f"[bold blue]{self.emoji} Shopee: consultando a API de afiliados por:[/bold blue] "
                f"[bold yellow]'{query}'[/bold yellow]"
            )
            resultados, erro_da_api = self._buscar_por_api(api, query, max_pages, max_items)

            if resultados:
                console.print(
                    f"[bold green]✓ Shopee (API de afiliados): {len(resultados)} produtos, "
                    f"já com o link afiliado.[/bold green]"
                )
                self.last_error = None
                return resultados

            console.print(
                f"[yellow]⚠️ {erro_da_api or 'Shopee: a API não devolveu produtos'} — "
                f"tentando pelo navegador...[/yellow]"
            )

        resultados = super().search_products(query, max_pages, max_items)

        # As duas rotas falharam: o portal precisa ver os dois motivos, senão
        # fica parecendo que só a raspagem foi tentada.
        if not resultados and erro_da_api:
            self.last_error = f"{erro_da_api} Depois disso, pelo navegador: {self.last_error}"

        return resultados

    def _buscar_por_api(self, api, query: str, max_pages: int, max_items: int) -> tuple[list[dict], str | None]:
        resultados: list[dict] = []
        erro = None

        for numero in range(1, max_pages + 1):
            nos, erro = api.buscar_produtos(query, pagina=numero, limite=min(max_items, 50))
            if erro or not nos:
                break

            produtos = [self._produto_da_api(no) for no in nos]
            self._acumular([p for p in produtos if p], resultados, max_items)

            if len(resultados) >= max_items:
                break

        return resultados, erro

    def _produto_da_api(self, no: dict) -> dict | None:
        """Converte um nó do productOfferV2 no formato padrão de produto."""
        if not isinstance(no, dict):
            return None

        titulo = str(no.get("productName") or "").strip()
        # offerLink já vem afiliado; productLink é o endereço cru da oferta.
        link = str(no.get("offerLink") or no.get("productLink") or "").strip()
        if not titulo or not link:
            return None

        desconto = self._inteiro(no.get("priceDiscountRate"))
        vendas = self._inteiro(no.get("sales"))

        destaque = None
        if desconto:
            destaque = f"{desconto}% OFF"
        elif vendas and vendas >= 100:
            destaque = f"{vendas}+ VENDIDOS"

        return self.produto(
            titulo=titulo,
            preco=clean_price(str(no.get("priceMin") or "")),
            # A API não devolve o preço cheio. Derivar um "De:" a partir da
            # porcentagem daria um número inventado na promoção do grupo, então
            # o desconto fica só no destaque.
            preco_original=None,
            vendedor=str(no.get("shopName") or "Shopee").strip(),
            destaque=destaque,
            link=link,
            imagem_url=str(no.get("imageUrl") or "").strip() or None,
        )

    @staticmethod
    def _inteiro(valor) -> int:
        try:
            return int(float(valor))
        except (TypeError, ValueError):
            return 0

    def page_url(self, query: str, page_number: int) -> str:
        # A Shopee conta as páginas a partir de zero.
        indice = max(0, page_number - 1)
        termo = urllib.parse.quote(query.strip())
        return f"{self.base_url}?keyword={termo}&page={indice}"

    # ------------------------------------------------------------------
    def parse_page(self, page) -> list[dict]:
        termo, indice = self._contexto_da_url(page.url)
        if not termo:
            # Sem 'keyword' na URL não estamos mais na página de busca: a
            # Shopee trocou a página por outra coisa no meio do caminho.
            self.last_error = (
                f"A Shopee trocou a busca por outra página ({page.url[:90]}) — "
                "é a tela de verificação de tráfego."
            )
            console.print(f"[yellow]⚠️ {self.last_error}[/yellow]")
            return []

        dados = self._consultar_api(page, termo, indice)
        falha = self._falha_da_api(dados)
        if falha:
            self.last_error = f"Shopee recusou a consulta de busca ({falha})."
            console.print(f"[yellow]⚠️ {self.last_error}[/yellow]")
        elif dados:
            produtos = self._produtos_da_api(dados)
            if produtos:
                return produtos

        console.print("[dim]Shopee: caindo para a leitura dos cards da página...[/dim]")
        produtos = self._ler_cards(page)

        # Nem a API nem o DOM devolveram nada: registra o porquê em vez de
        # deixar o portal anunciar um genérico "nenhum produto encontrado".
        if not produtos and not self.last_error:
            self.last_error = (
                "A Shopee carregou a página sem nenhum produto — normalmente é a "
                "tela de verificação de tráfego."
            )

        return produtos

    def _contexto_da_url(self, url: str) -> tuple[str, int]:
        """Recupera o termo e o número da página a partir da URL carregada."""
        try:
            consulta = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        except Exception:
            return "", 0

        termo = (consulta.get("keyword") or [""])[0]
        try:
            indice = int((consulta.get("page") or ["0"])[0])
        except ValueError:
            indice = 0
        return termo, max(0, indice)

    def _consultar_api(self, page, termo: str, indice: int):
        try:
            return page.evaluate(
                CONSULTA_API,
                [termo, indice * self.itens_por_pagina, self.itens_por_pagina],
            )
        except Exception as exc:
            # "Execution context was destroyed" aqui significa que a Shopee
            # redirecionou a página no meio da consulta — quase sempre para a
            # tela de verificação.
            self.last_error = f"Shopee: falha ao consultar a API interna ({str(exc).splitlines()[0][:120]})."
            console.print(f"[yellow]⚠️ {self.last_error}[/yellow]")
            return None

    @staticmethod
    def _falha_da_api(dados) -> str | None:
        """
        Reconhece a recusa da Shopee, que não usa status HTTP para isso.

        Bloqueada, a API devolve HTTP 200 com um corpo ofuscado do tipo
        {"error": 90309999, "0": 3, "5": false, ...}. Sem esta checagem o
        scraper trataria a recusa como "a busca não achou nada".
        """
        if not isinstance(dados, dict):
            return None
        if dados.get("erro"):
            return str(dados["erro"])
        if dados.get("error"):
            return f"código {dados['error']}"
        return None

    # ------------------------------------------------------------------
    def _produtos_da_api(self, dados: dict) -> list[dict]:
        itens = dados.get("items") if isinstance(dados, dict) else None
        if not isinstance(itens, list):
            return []

        produtos = []
        for entrada in itens:
            if not isinstance(entrada, dict):
                continue
            # Dependendo da versão da API o produto vem em item_basic ou solto.
            bruto = entrada.get("item_basic") or entrada.get("item") or entrada
            produto = self._montar_produto(bruto)
            if produto:
                produtos.append(produto)
        return produtos

    def _montar_produto(self, bruto: dict) -> dict | None:
        if not isinstance(bruto, dict):
            return None

        itemid = bruto.get("itemid")
        shopid = bruto.get("shopid")
        titulo = (bruto.get("name") or "").strip()
        if not itemid or not shopid or not titulo:
            return None

        preco = self._preco(bruto.get("price") or bruto.get("price_min"))
        preco_original = self._preco(bruto.get("price_before_discount"))
        if preco_original and preco and preco_original <= preco:
            preco_original = None

        desconto = bruto.get("raw_discount") or 0
        destaque = f"{int(desconto)}% OFF" if desconto else None
        if not destaque and bruto.get("is_official_shop"):
            destaque = "LOJA OFICIAL"

        imagem = bruto.get("image")
        imagem_url = f"https://down-br.img.susercontent.com/file/{imagem}" if imagem else None

        vendedor = bruto.get("shop_name") or bruto.get("shop_location") or "Shopee"

        return self.produto(
            titulo=titulo,
            preco=preco,
            preco_original=preco_original,
            vendedor=str(vendedor).strip(),
            frete_gratis=bool(bruto.get("show_free_shipping")),
            destaque=destaque,
            link=self._link(titulo, shopid, itemid),
            imagem_url=imagem_url,
        )

    @staticmethod
    def _preco(valor) -> float | None:
        try:
            numero = float(valor) / FATOR_PRECO
        except (TypeError, ValueError, ZeroDivisionError):
            return None
        return round(numero, 2) if numero > 0 else None

    @staticmethod
    def _link(titulo: str, shopid, itemid) -> str:
        """URL no formato que a própria Shopee usa (o texto antes de '-i.' é decorativo)."""
        slug = re.sub(r'[^\w\s-]', '', titulo.lower())
        slug = re.sub(r'[-\s]+', '-', slug).strip('-')[:80] or "produto"
        return f"https://shopee.com.br/{slug}-i.{shopid}.{itemid}"

    # ------------------------------------------------------------------
    # Plano B: cards renderizados
    # ------------------------------------------------------------------
    def _ler_cards(self, page) -> list[dict]:
        produtos: list[dict] = []
        vistos: set[str] = set()

        seletor = "li[data-sqe='item'], .shopee-search-item-result__item, a[href*='-i.']"
        for card in page.query_selector_all(seletor):
            try:
                link_elem = card if card.get_attribute("href") else card.query_selector("a[href*='-i.']")
                if not link_elem:
                    continue

                href = (link_elem.get_attribute("href") or "").split("?")[0]
                if href.startswith("/"):
                    href = f"https://shopee.com.br{href}"
                if not href or href in vistos:
                    continue

                texto = (card.inner_text() or "").strip()
                linhas = [linha.strip() for linha in texto.split("\n") if linha.strip()]
                titulo = next((linha for linha in linhas if len(linha) > 15), None)
                if not titulo:
                    continue

                precos = re.findall(r'R\$\s?[\d.,]+', texto)
                img_elem = card.query_selector("img")

                vistos.add(href)
                produtos.append(self.produto(
                    titulo=titulo,
                    preco=clean_price(precos[0]) if precos else None,
                    preco_original=clean_price(precos[1]) if len(precos) > 1 else None,
                    vendedor="Shopee",
                    frete_gratis="grátis" in texto.lower(),
                    link=href,
                    imagem_url=(img_elem.get_attribute("src") if img_elem else None),
                ))
            except Exception:
                continue

        return produtos
