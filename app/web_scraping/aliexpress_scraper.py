"""
Scraper de busca do AliExpress (loja brasileira, pt.aliexpress.com).

O AliExpress renderiza a vitrine no navegador, mas antes disso injeta o
resultado inteiro da busca num objeto JavaScript da própria página
(`window._dida_config_._init_data_`). Ler esse objeto é o caminho estável: os
nomes de classe dos cards são gerados por build e mudam sem aviso, enquanto o
JSON mantém os mesmos campos (`productId`, `prices`, `title`).

Como o objeto já vem no HTML da resposta, uma requisição comum resolve a maior
parte das buscas. O navegador entra quando o site decide exigir JavaScript.
"""

import json
import re
import sys
import urllib.parse

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from rich.console import Console

from base_scraper import MarketplaceScraper
from utils import clean_price

console = Console(force_terminal=True)

ATRIBUICAO_INIT_DATA = re.compile(r'_init_data_\s*=\s*\{')


class AliExpressScraper(MarketplaceScraper):
    nome = "AliExpress"
    perfil = "aliexpress"
    emoji = "🌐"
    espera_seletor = "a[href*='/item/']"

    base_url = "https://pt.aliexpress.com/w/wholesale-"

    def page_url(self, query: str, page_number: int) -> str:
        slug = re.sub(r'[^\w\s-]', '', query.strip().lower())
        slug = re.sub(r'[-\s]+', '-', slug).strip('-') or "produtos"
        termo = urllib.parse.quote(query.strip())
        return (
            f"{self.base_url}{slug}.html"
            f"?page={page_number}&SearchText={termo}&g=y"
        )

    # ------------------------------------------------------------------
    # Extração
    # ------------------------------------------------------------------
    def parse_html(self, html: str) -> list[dict]:
        estrutura = self._extrair_init_data(html or "")
        if not estrutura:
            return []
        return self._produtos_de(estrutura)

    def parse_page(self, page) -> list[dict]:
        # Dentro do navegador o objeto já está montado: pegá-lo direto evita
        # reparsear meio megabyte de HTML.
        try:
            estrutura = page.evaluate(
                "() => (window._dida_config_ && window._dida_config_._init_data_) || null"
            )
        except Exception:
            estrutura = None

        if estrutura:
            produtos = self._produtos_de(estrutura)
            if produtos:
                return produtos

        produtos = super().parse_page(page)
        if produtos:
            return produtos

        console.print("[dim]AliExpress: dados embutidos indisponíveis, lendo os cards...[/dim]")
        return self._ler_cards(page)

    def _produtos_de(self, estrutura) -> list[dict]:
        itens = self._localizar_itens(estrutura)
        produtos = [self._montar_produto(bruto) for bruto in itens]
        return [p for p in produtos if p]

    def _extrair_init_data(self, html: str) -> dict | None:
        """
        Recorta o objeto `_init_data_` do HTML.

        Ele é um literal JavaScript (`{ data: {...} }`, com a chave sem aspas),
        então não dá para jogar direto no json.loads: pegamos o objeto interno,
        que é JSON válido, casando as chaves na mão.
        """
        # A busca precisa ser pela ATRIBUIÇÃO (`_init_data_= {`): o nome
        # `_init_data_` também aparece antes disso dentro do bundle de
        # JavaScript, e cair naquela primeira ocorrência devolve lixo.
        for atribuicao in ATRIBUICAO_INIT_DATA.finditer(html):
            externo = atribuicao.end() - 1          # o `{` do literal externo
            interno = html.find("{", externo + 1)   # valor de `data:`, JSON válido
            if interno < 0:
                continue

            bruto = self._recortar_objeto(html, interno)
            if not bruto:
                continue

            try:
                return json.loads(bruto)
            except json.JSONDecodeError:
                continue

        return None

    @staticmethod
    def _recortar_objeto(texto: str, inicio: int) -> str | None:
        """Devolve o objeto JSON que começa em `inicio`, casando chaves."""
        profundidade = 0
        dentro_de_string = False
        escapado = False

        for pos in range(inicio, len(texto)):
            caractere = texto[pos]

            if dentro_de_string:
                if escapado:
                    escapado = False
                elif caractere == "\\":
                    escapado = True
                elif caractere == '"':
                    dentro_de_string = False
                continue

            if caractere == '"':
                dentro_de_string = True
            elif caractere == "{":
                profundidade += 1
            elif caractere == "}":
                profundidade -= 1
                if profundidade == 0:
                    return texto[inicio:pos + 1]

        return None

    def _localizar_itens(self, no, profundidade: int = 0) -> list[dict]:
        """Procura `itemList.content` em qualquer ponto da estrutura."""
        if profundidade > 10:
            return []

        if isinstance(no, dict):
            lista = no.get("itemList")
            if isinstance(lista, dict) and isinstance(lista.get("content"), list):
                return lista["content"]
            for filho in no.values():
                achado = self._localizar_itens(filho, profundidade + 1)
                if achado:
                    return achado
        elif isinstance(no, list):
            for filho in no[:20]:
                achado = self._localizar_itens(filho, profundidade + 1)
                if achado:
                    return achado

        return []

    def _montar_produto(self, bruto: dict) -> dict | None:
        if not isinstance(bruto, dict):
            return None

        product_id = bruto.get("productId") or bruto.get("redirectedId")
        titulo = ((bruto.get("title") or {}).get("displayTitle") or "").strip()
        if not product_id or not titulo:
            return None

        precos = bruto.get("prices") or {}
        venda = precos.get("salePrice") or {}
        original = precos.get("originalPrice") or {}

        preco = self._preco(venda)
        preco_original = self._preco(original)
        if preco_original and preco and preco_original <= preco:
            preco_original = None

        etiquetas = self._etiquetas(bruto)
        juntas = " | ".join(etiquetas).lower()

        loja = bruto.get("store") or {}
        vendedor = (loja.get("storeName") if isinstance(loja, dict) else None) or "AliExpress"

        imagem = ((bruto.get("image") or {}).get("imgUrl") or "").strip()
        if imagem.startswith("//"):
            imagem = f"https:{imagem}"

        return self.produto(
            titulo=titulo,
            preco=preco,
            preco_original=preco_original,
            vendedor=vendedor,
            frete_gratis=("grátis" in juntas or "gratis" in juntas or "free shipping" in juntas),
            destaque=self._destaque(venda, etiquetas),
            cupom=next((e for e in etiquetas if "cupom" in e.lower() or "coupon" in e.lower()), None),
            link=f"https://pt.aliexpress.com/item/{product_id}.html",
            imagem_url=imagem or None,
        )

    @staticmethod
    def _preco(bloco: dict) -> float | None:
        if not isinstance(bloco, dict):
            return None
        try:
            valor = float(bloco.get("minPrice"))
            if valor > 0:
                return round(valor, 2)
        except (TypeError, ValueError):
            pass
        return clean_price(bloco.get("formattedPrice") or "")

    @staticmethod
    def _etiquetas(bruto: dict) -> list[str]:
        """Textos dos selos do card (frete, cupom, 'Top vendas', parcelamento)."""
        textos = []
        for selo in bruto.get("sellingPoints") or []:
            if not isinstance(selo, dict):
                continue
            texto = (selo.get("tagContent") or {}).get("tagText")
            if texto:
                textos.append(str(texto).strip())

        vendas = (bruto.get("trade") or {}).get("tradeDesc")
        if vendas:
            textos.append(str(vendas).strip())
        return textos

    @staticmethod
    def _destaque(venda: dict, etiquetas: list[str]) -> str | None:
        desconto = venda.get("discount") if isinstance(venda, dict) else None
        try:
            desconto = int(desconto)
        except (TypeError, ValueError):
            desconto = 0

        if desconto > 0:
            return f"{desconto}% OFF"

        for etiqueta in etiquetas:
            if any(chave in etiqueta.lower() for chave in ("top vendas", "choice", "mais vendido")):
                return etiqueta
        return None

    # ------------------------------------------------------------------
    # Plano B: cards renderizados
    # ------------------------------------------------------------------
    def _ler_cards(self, page) -> list[dict]:
        produtos: list[dict] = []
        vistos: set[str] = set()

        for card in page.query_selector_all("div[class*='search-item-card-wrapper'], a[href*='/item/']"):
            try:
                link_elem = card if card.get_attribute("href") else card.query_selector("a[href*='/item/']")
                if not link_elem:
                    continue

                href = (link_elem.get_attribute("href") or "").split("?")[0]
                if href.startswith("//"):
                    href = f"https:{href}"
                if not href or href in vistos:
                    continue

                texto = card.inner_text().strip()
                if not texto:
                    continue

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
                    vendedor="AliExpress",
                    link=href,
                    imagem_url=(img_elem.get_attribute("src") if img_elem else None),
                ))
            except Exception:
                continue

        return produtos
