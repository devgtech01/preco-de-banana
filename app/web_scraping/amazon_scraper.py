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

class AmazonScraper:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self.base_url = "https://www.amazon.com.br/s?k="
        self.last_error: str | None = None

    def _extract_product_data(self, item_element) -> dict | None:
        """Extrai dados de um card de produto da Amazon Brasil."""
        try:
            # 1. Título
            title_elem = (
                item_element.query_selector("h2 a span") or
                item_element.query_selector("h2 span") or
                item_element.query_selector("h2")
            )
            if not title_elem:
                return None

            titulo = title_elem.inner_text().strip()
            if not titulo:
                return None

            # 2. Link do Produto com múltiplos fallbacks
            link_elem = (
                item_element.query_selector("h2 a") or
                item_element.query_selector("a.a-link-normal.s-line-clamp-2") or
                item_element.query_selector("a.a-link-normal.s-no-outline") or
                item_element.query_selector("a.a-link-normal[href*='/dp/']") or
                item_element.query_selector("a.a-link-normal[href*='/sspa/']") or
                item_element.query_selector("a.a-link-normal")
            )
            # get_attribute devolve None quando o atributo não existe: sem o
            # fallback para "" isso virava AttributeError e o produto era
            # descartado em silêncio pelo except lá embaixo.
            href = (link_elem.get_attribute("href") if link_elem else "") or ""
            if href.startswith("/"):
                link = f"https://www.amazon.com.br{href}"
            else:
                link = href

            if not link:
                return None

            # 3. Preço Atual com múltiplos fallbacks (Offscreen text + Whole/Fraction parsing)
            price_offscreen = item_element.query_selector("span.a-price span.a-offscreen")
            price_whole = item_element.query_selector(".a-price-whole")
            price_fraction = item_element.query_selector(".a-price-fraction")

            preco = None
            if price_offscreen:
                preco = clean_price(price_offscreen.inner_text())

            if preco is None and price_whole:
                pw = price_whole.inner_text().replace("\n", "").replace(".", "").replace(",", "").strip()
                pf = price_fraction.inner_text().replace(".", "").replace(",", "").strip() if price_fraction else "00"
                if pw:
                    preco = clean_price(f"{pw},{pf}")

            # 4. Preço Original (se houver desconto)
            original_price_elem = item_element.query_selector(".a-text-price .a-offscreen")
            preco_original = clean_price(original_price_elem.inner_text()) if original_price_elem else None

            # 5. Vendedor / Loja
            seller_elem = item_element.query_selector(".a-row.a-size-base.a-color-secondary .a-size-base")
            vendedor = seller_elem.inner_text().strip() if seller_elem else "Amazon Brasil"

            # 6. Frete Grátis / Prime
            prime_elem = (
                item_element.query_selector(".a-icon-prime") or
                item_element.query_selector("i[aria-label='Amazon Prime']")
            )
            shipping_text_elem = item_element.query_selector(".s-align-children-center .a-color-base")
            
            frete_gratis = bool(prime_elem)
            if not frete_gratis and shipping_text_elem:
                txt = shipping_text_elem.inner_text().lower()
                if "frete grátis" in txt or "grátis" in txt:
                    frete_gratis = True

            # 7. Condição
            condicao = "Novo"

            # 8. Imagem
            img_elem = item_element.query_selector("img.s-image")
            imagem_url = img_elem.get_attribute("src") if img_elem else None

            # 9. Destaque / Badges da Amazon (ex: "Mais vendido", "Escolha da Amazon")
            badge_elem = (
                item_element.query_selector(".a-badge-text") or
                item_element.query_selector("span[id*='amazons-choice']") or
                item_element.query_selector(".a-badge-label") or
                item_element.query_selector(".s-coupon-highlight-color")
            )
            destaque = badge_elem.inner_text().strip() if badge_elem else None

            if not destaque and frete_gratis:
                destaque = "PRIME - FRETE GRÁTIS"

            # 10. Cupom (ex: "Cupom de R$ 50 aplicado ao finalizar a compra")
            coupon_elem = (
                item_element.query_selector(".s-coupon-highlight-color") or
                item_element.query_selector("span.a-color-base.a-text-bold")
            )
            cupom = None
            if coupon_elem:
                raw_txt = coupon_elem.inner_text().strip()
                if "cupom" in raw_txt.lower():
                    cupom = raw_txt

            return {
                "plataforma": "Amazon Brasil",
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

    ITEM_SELECTOR = "div[data-component-type='s-search-result']"

    def _collect_page(self, page: Page, results: list[dict], max_items: int) -> None:
        """Extrai os cards visíveis na página atual, ignorando links repetidos."""
        items = page.query_selector_all(self.ITEM_SELECTOR)
        known_links = {r["link"] for r in results if r.get("link")}
        added = 0

        for item in items:
            if len(results) >= max_items:
                break
            data = self._extract_product_data(item)
            if not data or not data.get("titulo") or data["link"] in known_links:
                continue
            known_links.add(data["link"])
            results.append(data)
            added += 1

        if items and added == 0 and not results:
            console.print(
                f"[yellow]⚠️ {len(items)} cards encontrados, 0 extraídos — "
                f"os seletores da Amazon provavelmente mudaram.[/yellow]"
            )

    def _scrape_once(self, page: Page, start_url: str, max_pages: int, max_items: int) -> list[dict]:
        """Percorre a paginação da busca. Levanta BlockedError se houver desafio."""
        results: list[dict] = []
        url = start_url
        current_page = 1

        while current_page <= max_pages and len(results) < max_items:
            console.print(f"[cyan]🌐 Carregando Amazon página {current_page}/{max_pages}...[/cyan]")

            response = None
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except PlaywrightTimeoutError:
                console.print(f"[yellow]⚠️ Timeout na Amazon página {current_page}.[/yellow]")

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

            next_button = page.query_selector("a.s-pagination-next")
            href = next_button.get_attribute("href") if next_button else None
            if not href:
                break

            url = f"https://www.amazon.com.br{href}" if href.startswith("/") else href
            current_page += 1
            antibot.human_pause(1.5, 4.0)

        return results

    def search_products(self, query: str, max_pages: int = 1, max_items: int = 100) -> list[dict]:
        """
        Navega pela Amazon Brasil com fingerprint mascarado, sessão persistente
        e retentativa com backoff quando o site devolve o CAPTCHA de robô.
        """
        self.last_error = None
        target_url = f"{self.base_url}{urllib.parse.quote(query)}"

        console.print(f"[bold orange3]📦 Iniciando busca na Amazon por:[/bold orange3] [bold yellow]'{query}'[/bold yellow]")
        start_time = time.time()

        results: list[dict] = []
        attempts = antibot.max_attempts()

        for attempt in range(attempts):
            try:
                with sync_playwright() as p:
                    with antibot.browser_session(p, "amazon", self.headless) as page:
                        results = self._scrape_once(page, target_url, max_pages, max_items)
                break

            except antibot.BlockedError as exc:
                self.last_error = f"Amazon bloqueou o acesso: {exc}"
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
            console.print(f"[bold green]✓ Busca na Amazon concluída em {elapsed}s. Total: {len(results)} produtos.[/bold green]")
        else:
            console.print(f"[bold red]✗ Amazon retornou 0 produtos em {elapsed}s. Motivo: {self.last_error or 'nenhum card na página'}[/bold red]")

        return results
