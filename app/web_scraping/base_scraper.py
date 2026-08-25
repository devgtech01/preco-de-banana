"""
Base compartilhada pelos scrapers de marketplace adicionados depois do
Mercado Livre e da Amazon (Shopee, AliExpress e KaBuM!).

As duas lojas antigas têm classe própria porque cada uma segue o botão
"próxima página" do próprio site. As três novas paginam por número na URL e
entregam o catálogo pronto em JSON embutido na página, então todo o fluxo mora
aqui: tentativa por HTTP simples, queda para o Firefox mascarado quando o HTTP
não basta, retentativa com backoff, deduplicação por link e exatamente o mesmo
formato de saída dos scrapers antigos (o portal e o bot não precisam saber de
qual loja veio o produto além do campo `plataforma`).

Por que HTTP primeiro: abrir o Firefox custa de 5 a 15 segundos — bem mais no
modo pendrive, lendo de uma porta USB — e cada navegador aberto é memória que a
máquina do usuário precisa ter. Quando uma requisição comum resolve, ela vem
antes; o navegador fica como plano B.
"""

import sys
import time

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

import requests
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError
from rich.console import Console

import antibot

console = Console(force_terminal=True)


class MarketplaceScraper:
    """
    Esqueleto de um scraper de busca. Cada loja implementa três coisas:

      * `page_url(query, numero_da_pagina)` — o endereço da enésima página;
      * `parse_html(html)` — a extração a partir do HTML cru;
      * `parse_page(page)` — a extração com a página já viva no Playwright
        (o padrão reaproveita `parse_html`, o que basta para quem embute JSON).
    """

    nome = "Marketplace"
    perfil = "generic"                  # arquivo de sessão em data/profiles
    emoji = "🛍️"
    suporta_http = True                 # False obriga a passar pelo navegador
    espera_seletor: str | None = None   # opcional: espera a página pintar
    limite_tentativas: int | None = None  # teto próprio (None = SCRAPER_MAX_ATTEMPTS)
    dica_bloqueio: str | None = None    # sugestão anexada ao erro quando volta vazio

    def __init__(self, headless: bool = True):
        self.headless = headless
        self.last_error: str | None = None

    # ------------------------------------------------------------------
    # Contrato implementado por cada loja
    # ------------------------------------------------------------------
    def page_url(self, query: str, page_number: int) -> str:
        raise NotImplementedError

    def parse_html(self, html: str) -> list[dict]:
        return []

    def parse_page(self, page: Page) -> list[dict]:
        try:
            return self.parse_html(page.content())
        except Exception as exc:
            console.print(f"[yellow]⚠️ {self.nome}: falha ao ler a página ({exc}).[/yellow]")
            return []

    # ------------------------------------------------------------------
    # Utilitários de saída
    # ------------------------------------------------------------------
    def produto(self, **campos) -> dict:
        """
        Monta um produto no formato que o portal e o bot já entendem.

        Manter os mesmos nomes de campo dos scrapers antigos é o que permite a
        tabela do portal, a exportação CSV/JSON e o /api/post-deal do bot
        funcionarem para as lojas novas sem nenhuma ramificação.
        """
        base = {
            "plataforma": self.nome,
            "titulo": None,
            "preco": None,
            "preco_original": None,
            "vendedor": None,
            "frete_gratis": False,
            "condicao": "Novo",
            "destaque": None,
            "cupom": None,
            "link": None,
            "imagem_url": None,
        }
        base.update(campos)
        return base

    def _acumular(self, novos: list[dict], resultados: list[dict], max_items: int) -> int:
        """Adiciona os produtos ainda não vistos e devolve quantos entraram."""
        conhecidos = {r["link"] for r in resultados if r.get("link")}
        adicionados = 0

        for item in novos or []:
            if len(resultados) >= max_items:
                break
            if not item or not item.get("titulo") or not item.get("link"):
                continue
            if item["link"] in conhecidos:
                continue
            conhecidos.add(item["link"])
            resultados.append(item)
            adicionados += 1

        return adicionados

    # ------------------------------------------------------------------
    # Caminho 1: requisição HTTP comum
    # ------------------------------------------------------------------
    def _buscar_http(self, query: str, max_pages: int, max_items: int) -> list[dict]:
        resultados: list[dict] = []
        sessao = requests.Session()
        proxies = antibot.http_proxies()

        for numero in range(1, max_pages + 1):
            url = self.page_url(query, numero)
            console.print(f"[cyan]🌐 {self.nome}: página {numero}/{max_pages} (HTTP)...[/cyan]")

            try:
                resposta = sessao.get(
                    url,
                    headers=antibot.http_headers(),
                    proxies=proxies,
                    timeout=30,
                    allow_redirects=True,
                )
            except Exception as exc:
                self.last_error = f"{self.nome}: falha na requisição ({exc})."
                break

            motivo = antibot.detect_block_text(resposta.text, resposta.status_code)
            if motivo:
                self.last_error = f"{self.nome} respondeu com verificação: {motivo}"
                break

            itens = self.parse_html(resposta.text)
            if not itens:
                # Sem itens na primeira página o mais provável é que o layout
                # tenha mudado ou que a loja exija JavaScript: vale o navegador.
                break

            self._acumular(itens, resultados, max_items)
            if len(resultados) >= max_items:
                break
            antibot.human_pause(0.8, 2.0)

        sessao.close()
        return resultados

    # ------------------------------------------------------------------
    # Caminho 2: Firefox mascarado (mesma proteção de ML e Amazon)
    # ------------------------------------------------------------------
    def _percorrer_paginas(self, page: Page, query: str, max_pages: int, max_items: int) -> list[dict]:
        resultados: list[dict] = []

        for numero in range(1, max_pages + 1):
            url = self.page_url(query, numero)
            console.print(f"[cyan]🌐 {self.nome}: página {numero}/{max_pages} (navegador)...[/cyan]")

            resposta = None
            try:
                resposta = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except PlaywrightTimeoutError:
                console.print(f"[yellow]⚠️ Timeout ao carregar {self.nome} na página {numero}.[/yellow]")

            motivo = antibot.detect_block(page, resposta)
            if motivo:
                raise antibot.BlockedError(motivo)

            if self.espera_seletor:
                try:
                    page.wait_for_selector(self.espera_seletor, timeout=12000)
                except PlaywrightTimeoutError:
                    console.print("[yellow]⚠️ Nenhum card apareceu no tempo esperado.[/yellow]")

            antibot.human_scroll(page)

            # Segunda checagem de bloqueio. A primeira roda logo após o goto,
            # mas o desafio costuma chegar depois, por redirecionamento do
            # próprio JavaScript da loja — sem esta linha o scraper conclui
            # "o layout mudou" quando na verdade nunca chegou na busca.
            motivo = antibot.detect_block(page)
            if motivo:
                raise antibot.BlockedError(motivo)

            itens = self.parse_page(page)

            if not itens and not resultados:
                console.print(
                    f"[yellow]⚠️ {self.nome}: a página carregou mas nenhum produto foi extraído — "
                    f"o layout provavelmente mudou.[/yellow]"
                )

            self._acumular(itens, resultados, max_items)
            if len(resultados) >= max_items or not itens:
                break
            antibot.human_pause(1.5, 4.0)

        return resultados

    def _buscar_navegador(self, query: str, max_pages: int, max_items: int) -> list[dict]:
        resultados: list[dict] = []
        tentativas = antibot.max_attempts()
        if self.limite_tentativas:
            tentativas = min(tentativas, self.limite_tentativas)

        for tentativa in range(tentativas):
            try:
                with sync_playwright() as p:
                    with antibot.browser_session(p, self.perfil, self.headless) as page:
                        resultados = self._percorrer_paginas(page, query, max_pages, max_items)
                break

            except antibot.BlockedError as exc:
                self.last_error = f"{self.nome} bloqueou o acesso: {exc}"
                console.print(
                    f"[bold red]🚫 {self.last_error} (tentativa {tentativa + 1}/{tentativas})[/bold red]"
                )
                if tentativa < tentativas - 1:
                    antibot.backoff_sleep(tentativa)

            except Exception as exc:
                self.last_error = f"{self.nome} — falha no navegador: {exc}"
                console.print(f"[bold red]💥 {self.last_error}[/bold red]")
                break

        return resultados

    # ------------------------------------------------------------------
    def search_products(self, query: str, max_pages: int = 1, max_items: int = 100) -> list[dict]:
        """Busca na loja, com o mesmo contrato de MercadoLivreScraper/AmazonScraper."""
        self.last_error = None
        console.print(
            f"[bold blue]{self.emoji} Iniciando busca no {self.nome} por:[/bold blue] "
            f"[bold yellow]'{query}'[/bold yellow]"
        )
        inicio = time.time()

        resultados: list[dict] = []
        if self.suporta_http and antibot.http_first_enabled():
            resultados = self._buscar_http(query, max_pages, max_items)

        if not resultados:
            if self.suporta_http:
                console.print(f"[dim]{self.nome}: HTTP não resolveu, abrindo o navegador...[/dim]")
            # O erro do caminho HTTP não vale como diagnóstico se o navegador
            # ainda vai tentar: só o resultado final interessa ao usuário.
            self.last_error = None
            resultados = self._buscar_navegador(query, max_pages, max_items)

        import gc
        gc.collect()

        decorrido = round(time.time() - inicio, 2)
        if resultados:
            console.print(
                f"[bold green]✓ Busca no {self.nome} concluída em {decorrido}s. "
                f"Total: {len(resultados)} produtos.[/bold green]"
            )
        else:
            motivo = self.last_error or f"{self.nome}: nenhum produto na página"
            if self.dica_bloqueio:
                separador = "" if motivo.endswith((".", "!", "?")) else "."
                motivo = f"{motivo}{separador} {self.dica_bloqueio}"
            self.last_error = motivo
            console.print(
                f"[bold red]✗ {self.nome} retornou 0 produtos em {decorrido}s. "
                f"Motivo: {motivo}[/bold red]"
            )

        return resultados
