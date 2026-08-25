"""
Scraper de busca da KaBuM!.

A KaBuM! é um site Next.js: a página de busca já vem do servidor com o catálogo
inteiro dentro do <script id="__NEXT_DATA__">. Isso é muito mais confiável do
que ler os cards do HTML — os nomes de classe do site mudam a cada deploy, o
JSON não — e permite que a busca funcione com uma requisição comum, sem abrir o
navegador. Os seletores do DOM ficam só como último recurso.
"""

import json
import re
import sys
import urllib.parse

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from rich.console import Console

from base_scraper import MarketplaceScraper

console = Console(force_terminal=True)

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
    re.DOTALL,
)


class KabumScraper(MarketplaceScraper):
    nome = "KaBuM!"
    perfil = "kabum"
    emoji = "💙"
    espera_seletor = "article.productCard, [class*='productCard'], #__NEXT_DATA__"

    base_url = "https://www.kabum.com.br/busca/"
    itens_por_pagina = 20

    def page_url(self, query: str, page_number: int) -> str:
        termo = urllib.parse.quote(query.strip())
        return (
            f"{self.base_url}{termo}"
            f"?page_number={page_number}&page_size={self.itens_por_pagina}"
        )

    # ------------------------------------------------------------------
    # Extração
    # ------------------------------------------------------------------
    def parse_html(self, html: str) -> list[dict]:
        match = NEXT_DATA_RE.search(html or "")
        if not match:
            return []

        try:
            dados = json.loads(match.group(1))
        except json.JSONDecodeError:
            return []

        catalogo = self._localizar_catalogo(dados)
        produtos = [self._montar_produto(bruto) for bruto in catalogo]
        return [p for p in produtos if p]

    def _localizar_catalogo(self, dados: dict) -> list[dict]:
        """
        Encontra a lista de produtos dentro do __NEXT_DATA__.

        O caminho conhecido vem primeiro; a varredura genérica existe porque a
        KaBuM! já reorganizou essa estrutura antes e um scraper que só conhece
        um caminho fixo quebra em silêncio quando isso acontece de novo.
        """
        try:
            catalogo = dados["props"]["pageProps"]["data"]["catalogServer"]["data"]
            if isinstance(catalogo, list) and catalogo:
                return catalogo
        except (KeyError, TypeError):
            pass

        encontrado: list[dict] = []

        def varrer(no, profundidade=0):
            if encontrado or profundidade > 8:
                return
            if isinstance(no, list):
                if no and isinstance(no[0], dict) and {"name", "friendlyName"} <= set(no[0]):
                    encontrado.extend(no)
                    return
                for filho in no[:20]:
                    varrer(filho, profundidade + 1)
            elif isinstance(no, dict):
                for filho in no.values():
                    varrer(filho, profundidade + 1)

        varrer(dados)
        return encontrado

    def _montar_produto(self, bruto: dict) -> dict | None:
        if not isinstance(bruto, dict):
            return None

        codigo = bruto.get("code")
        nome_amigavel = bruto.get("friendlyName")
        titulo = (bruto.get("name") or "").strip()
        if not codigo or not nome_amigavel or not titulo:
            return None

        # Produto esgotado não serve para divulgar: o grupo recebe um link morto.
        if bruto.get("available") is False:
            return None

        preco_cheio = self._numero(bruto.get("price"))
        preco_desconto = self._numero(bruto.get("priceWithDiscount"))
        preco_antigo = self._numero(bruto.get("oldPrice"))

        preco = preco_desconto or preco_cheio
        preco_original = None
        for candidato in (preco_cheio, preco_antigo):
            if candidato and preco and candidato > preco:
                preco_original = candidato
                break

        flags = bruto.get("flags") or {}
        frete_gratis = bool(flags.get("isFreeShipping") or flags.get("isFreeShippingPrime"))

        fabricante = bruto.get("manufacturer")
        if isinstance(fabricante, dict):
            fabricante = fabricante.get("name")
        vendedor = (bruto.get("sellerName") or fabricante or "KaBuM!").strip()

        return self.produto(
            titulo=titulo,
            preco=preco,
            preco_original=preco_original,
            vendedor=vendedor,
            frete_gratis=frete_gratis,
            condicao="Openbox" if flags.get("isOpenbox") else "Novo",
            destaque=self._destaque(bruto, flags),
            cupom=self._cupom(bruto.get("stamps")),
            link=f"https://www.kabum.com.br/produto/{codigo}/{nome_amigavel}",
            imagem_url=bruto.get("image") or None,
        )

    def _destaque(self, bruto: dict, flags: dict) -> str | None:
        desconto = self._numero(bruto.get("discountPercentage"))
        if desconto and desconto > 0:
            return f"{int(desconto)}% OFF"
        if flags.get("isFlash"):
            return "ENTREGA FLASH"
        if flags.get("isOffer"):
            return "OFERTA DO DIA"
        if flags.get("isPrime"):
            return "PRIME NINJA"
        return None

    def _cupom(self, stamps) -> str | None:
        """Os selos da KaBuM! carregam o cupom promocional (ex: 'CUPOM GAMER10')."""
        if isinstance(stamps, dict):
            stamps = [stamps]
        if not isinstance(stamps, list):
            return None

        for selo in stamps:
            if not isinstance(selo, dict):
                continue
            texto = (selo.get("title") or selo.get("name") or "").strip()
            if texto and "cupom" in texto.lower():
                return texto
        return None

    @staticmethod
    def _numero(valor) -> float | None:
        try:
            numero = float(valor)
        except (TypeError, ValueError):
            return None
        return numero if numero > 0 else None

    # ------------------------------------------------------------------
    # Plano B: ler os cards renderizados (só se o __NEXT_DATA__ sumir)
    # ------------------------------------------------------------------
    def parse_page(self, page) -> list[dict]:
        itens = super().parse_page(page)
        if itens:
            return itens

        console.print("[dim]KaBuM!: __NEXT_DATA__ ausente, lendo os cards da página...[/dim]")
        from utils import clean_price

        produtos: list[dict] = []
        for card in page.query_selector_all("article.productCard, [class*='productCard']"):
            try:
                link_elem = card.query_selector("a[href*='/produto/']")
                titulo_elem = card.query_selector("[class*='nameCard'], h2, h3, span[title]")
                if not link_elem or not titulo_elem:
                    continue

                href = link_elem.get_attribute("href") or ""
                if href.startswith("/"):
                    href = f"https://www.kabum.com.br{href}"

                preco_elem = card.query_selector("[class*='priceCard'], [class*='finalPrice']")
                antigo_elem = card.query_selector("[class*='oldPrice'], s")
                img_elem = card.query_selector("img")

                produtos.append(self.produto(
                    titulo=titulo_elem.inner_text().strip(),
                    preco=clean_price(preco_elem.inner_text()) if preco_elem else None,
                    preco_original=clean_price(antigo_elem.inner_text()) if antigo_elem else None,
                    vendedor="KaBuM!",
                    link=href,
                    imagem_url=(img_elem.get_attribute("src") if img_elem else None),
                ))
            except Exception:
                continue

        return produtos
