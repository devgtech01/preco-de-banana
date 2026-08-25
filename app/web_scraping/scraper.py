import re
import time
import urllib.parse
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError
from rich.console import Console

import antibot
from utils import clean_price

console = Console(force_terminal=True)

class MercadoLivreScraper:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self.base_url = "https://lista.mercadolivre.com.br/"
        self.last_error: str | None = None

    def _slugify_query(self, query: str) -> str:
        """Converte a query para o formato de slug limpo aceito pelo Mercado Livre."""
        cleaned = re.sub(r'[^\w\s-]', '', query.strip().lower())
        return re.sub(r'[-\s]+', '-', cleaned)

    def _extract_product_data(self, item_element) -> dict | None:
        """Extrai os dados brutos de um card de produto no Mercado Livre com seletores abrangentes."""
        try:
            # 1. Título
            title_elem = (
                item_element.query_selector(".poly-component__title") or
                item_element.query_selector("a.poly-component__title") or
                item_element.query_selector("h2.poly-box") or
                item_element.query_selector(".poly-box") or
                item_element.query_selector("h2.ui-search-item__title") or
                item_element.query_selector("a.ui-search-item__group__element") or
                item_element.query_selector("h2.ui-search-result__content-title") or
                item_element.query_selector(".ui-search-item__title") or
                item_element.query_selector("h2") or
                item_element.query_selector("h3")
            )
            if not title_elem:
                return None
            
            titulo = title_elem.inner_text().strip()
            if not titulo or len(titulo) < 3:
                return None

            # 2. Link do Produto
            link_elem = (
                item_element.query_selector("a.poly-component__title") or
                item_element.query_selector("a.ui-search-link") or
                item_element.query_selector("a.ui-search-item__group__element") or
                item_element.query_selector("a.ui-search-result__content-wrapper") or
                item_element.query_selector("a[href*='mercadolivre.com']") or
                item_element.query_selector("a")
            )
            link = link_elem.get_attribute("href") if link_elem else ""
            if link and link.startswith("//"):
                link = "https:" + link

            if not link:
                return None

            # 3. Preço Atual
            price_fraction_elem = (
                item_element.query_selector(".poly-price__current .andes-money-amount__fraction") or
                item_element.query_selector(".ui-search-price__part--medium .andes-money-amount__fraction") or
                item_element.query_selector(".andes-money-amount__fraction")
            )
            price_cents_elem = (
                item_element.query_selector(".poly-price__current .andes-money-amount__cents") or
                item_element.query_selector(".ui-search-price__part--medium .andes-money-amount__cents") or
                item_element.query_selector(".andes-money-amount__cents")
            )

            fraction_str = price_fraction_elem.inner_text().strip() if price_fraction_elem else ""
            cents_str = price_cents_elem.inner_text().strip() if price_cents_elem else "00"

            preco = clean_price(f"{fraction_str},{cents_str}") if fraction_str else None

            # 4. Preço Original (se houver desconto)
            original_price_elem = (
                item_element.query_selector("s.andes-money-amount .andes-money-amount__fraction") or
                item_element.query_selector(".poly-price__original .andes-money-amount__fraction") or
                item_element.query_selector(".ui-search-price__part--original .andes-money-amount__fraction")
            )
            preco_original = clean_price(original_price_elem.inner_text()) if original_price_elem else None

            # 5. Vendedor / Loja Oficial
            seller_elem = (
                item_element.query_selector(".poly-component__seller") or
                item_element.query_selector(".ui-search-official-store-label") or
                item_element.query_selector(".ui-search-item__group__element--seller")
            )
            vendedor = seller_elem.inner_text().replace("por", "").strip() if seller_elem else None

            # 6. Frete Grátis
            shipping_elem = (
                item_element.query_selector(".poly-component__shipping") or
                item_element.query_selector(".ui-search-item__shipping--free") or
                item_element.query_selector(".poly-shipping")
            )
            frete_gratis = bool(shipping_elem and "grátis" in shipping_elem.inner_text().lower())

            # 7. Condição (Novo / Usado)
            condition_elem = item_element.query_selector(".ui-search-item__group__element--condition")
            condicao = condition_elem.inner_text().strip() if condition_elem else "Novo"

            # 8. Imagem
            img_elem = (
                item_element.query_selector("img.poly-component__picture") or
                item_element.query_selector("img.ui-search-result-image__element") or
                item_element.query_selector("img[data-src]") or
                item_element.query_selector("img[src]") or
                item_element.query_selector("img")
            )
            imagem_url = None
            if img_elem:
                imagem_url = img_elem.get_attribute("data-src") or img_elem.get_attribute("src")

            # 9. Destaque / Tag de Promoção
            badge_elem = (
                item_element.query_selector(".poly-component__badge") or
                item_element.query_selector(".poly-badge") or
                item_element.query_selector(".poly-component__promotion_custom") or
                item_element.query_selector(".ui-search-item__highlight-label") or
                item_element.query_selector(".ui-search-item__group__element--highlight") or
                item_element.query_selector(".poly-component__highlight") or
                item_element.query_selector(".poly-component__tag")
            )
            destaque = badge_elem.inner_text().strip() if badge_elem else None
            
            if not destaque:
                extra_badges = item_element.query_selector_all("*[class*='badge'], *[class*='highlight'], *[class*='tag'], *[class*='promotion']")
                for b in extra_badges:
                    txt = b.inner_text().strip()
                    if txt and any(k in txt.lower() for k in ["imperdível", "oferta", "mais vendido", "recomendado", "desconto"]):
                        destaque = txt
                        break

            # 10. Cupom Elegível
            coupon_elem = (
                item_element.query_selector(".poly-component__coupons") or
                item_element.query_selector(".poly-component__coupon") or
                item_element.query_selector(".poly-coupon") or
                item_element.query_selector(".ui-search-item__coupon") or
                item_element.query_selector("*[class*='coupon']")
            )
            cupom = None
            if coupon_elem:
                raw_cupom = coupon_elem.inner_text().strip()
                if raw_cupom:
                    cupom = " ".join(raw_cupom.split())

            return {
                "plataforma": "Mercado Livre",
                "titulo": titulo,
                "preco": preco,
                "preco_original": preco_original,
                "vendedor": vendedor,
                "frete_gratis": frete_gratis,
                "condicao": condicao,
                "destaque": destaque,
                "cupom": cupom,
                "link": link,
                "imagem_url": imagem_url
            }

        except Exception:
            return None

    ITEM_SELECTOR = (
        "li.ui-search-layout__item, .ui-search-layout__item, .poly-card, div.poly-card, "
        "article.poly-card, div.ui-search-result, div.ui-search-result__wrapper, "
        ".ui-search-result, div.poly-card__content"
    )

    def _collect_page(self, page: Page, results: list[dict], max_items: int) -> int:
        """Extrai os cards visíveis na página atual e devolve quantos foram novos."""
        items = page.query_selector_all(self.ITEM_SELECTOR)
        known_links = {r["link"] for r in results if r.get("link")}
        added = 0

        for item in items:
            if len(results) >= max_items:
                break
            data = self._extract_product_data(item)
            if not data or not data.get("titulo"):
                continue
            if data["link"] in known_links:
                continue
            known_links.add(data["link"])
            results.append(data)
            added += 1

        if items and added == 0 and not results:
            # Cards existem mas nenhum produto saiu: os seletores mudaram.
            console.print(
                f"[yellow]⚠️ {len(items)} cards encontrados, 0 extraídos — "
                f"os seletores do Mercado Livre provavelmente mudaram.[/yellow]"
            )

        return added

    def _scrape_once(self, page: Page, start_url: str, max_pages: int, max_items: int) -> list[dict]:
        """Percorre a paginação a partir de uma URL. Levanta BlockedError se houver desafio."""
        results: list[dict] = []
        url = start_url
        current_page = 1

        while current_page <= max_pages and len(results) < max_items:
            console.print(f"[cyan]🌐 Carregando página {current_page}/{max_pages}...[/cyan]")

            response = None
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except PlaywrightTimeoutError:
                console.print(f"[yellow]⚠️ Timeout ao carregar a página {current_page}.[/yellow]")

            reason = antibot.detect_block(page, response)
            if reason:
                raise antibot.BlockedError(reason)

            try:
                page.wait_for_selector(self.ITEM_SELECTOR, timeout=10000)
            except PlaywrightTimeoutError:
                console.print("[yellow]⚠️ Nenhum card apareceu no tempo esperado.[/yellow]")

            antibot.human_scroll(page)
            self._collect_page(page, results, max_items)

            if len(results) >= max_items or current_page >= max_pages:
                break

            next_button = (
                page.query_selector("a.andes-pagination__link[title='Próxima']") or
                page.query_selector("li.andes-pagination__button--next a") or
                page.query_selector("a.andes-pagination__link--next")
            )
            next_url = next_button.get_attribute("href") if next_button else None
            if not next_url:
                break

            url = next_url
            current_page += 1
            antibot.human_pause(1.5, 4.0)

        return results

    def search_products(self, query: str, max_pages: int = 1, max_items: int = 100) -> list[dict]:
        """
        Navega pelo Mercado Livre com fingerprint mascarado, sessão persistente
        e retentativa com backoff quando o site devolve desafio de segurança.
        """
        self.last_error = None
        slug = self._slugify_query(query)
        encoded = urllib.parse.quote(query)

        # Variantes de URL usadas como *fallback*: a seguinte só é tentada se a
        # anterior não devolveu nada. O código antigo percorria todas sempre,
        # triplicando requisições e o risco de bloqueio.
        urls_to_try = [
            f"{self.base_url}{slug}",
            f"{self.base_url}{slug}_NoIndex_True",
            f"{self.base_url}{encoded}",
        ]

        console.print(f"[bold blue]⚡ Iniciando busca no Mercado Livre por:[/bold blue] [bold yellow]'{query}'[/bold yellow]")
        start_time = time.time()

        results: list[dict] = []
        attempts = antibot.max_attempts()

        for attempt in range(attempts):
            try:
                with sync_playwright() as p:
                    with antibot.browser_session(p, "mercadolivre", self.headless) as page:
                        for target_url in urls_to_try:
                            results = self._scrape_once(page, target_url, max_pages, max_items)
                            if results:
                                break
                            console.print("[dim]Sem resultados nessa URL, tentando variante...[/dim]")
                            antibot.human_pause(1.0, 2.5)
                break

            except antibot.BlockedError as exc:
                self.last_error = f"Mercado Livre bloqueou o acesso: {exc}"
                console.print(f"[bold red]🚫 {self.last_error} (tentativa {attempt + 1}/{attempts})[/bold red]")
                if attempt < attempts - 1:
                    antibot.backoff_sleep(attempt)

            except Exception as exc:
                self.last_error = f"Falha no navegador: {exc}"
                console.print(f"[bold red]💥 {self.last_error}[/bold red]")
                break

        import gc
        gc.collect()

        elapsed = round(time.time() - start_time, 2)
        if results:
            console.print(f"[bold green]✓ Busca Mercado Livre concluída em {elapsed}s. Total: {len(results)} produtos.[/bold green]")
        else:
            console.print(f"[bold red]✗ Mercado Livre retornou 0 produtos em {elapsed}s. Motivo: {self.last_error or 'nenhum card na página'}[/bold red]")

        return results
