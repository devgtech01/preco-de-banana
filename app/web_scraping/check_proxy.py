"""
Diagnóstico do proxy e do bloqueio nos marketplaces.

Responde três perguntas, na ordem em que elas importam:
  1. Por qual IP eu estou saindo, e ele é residencial ou de datacenter?
  2. O Mercado Livre me deixa entrar?
  3. A Amazon me deixa entrar?

Usa exatamente o mesmo caminho de código dos scrapers (mesmo fingerprint,
mesmo proxy, mesma sessão), então o resultado aqui é o resultado real.

Uso:
    python check_proxy.py

Dentro do container:
    docker compose exec scraper-portal python check_proxy.py
"""

import json
import os
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from dotenv import load_dotenv

base_dir = os.path.dirname(os.path.abspath(__file__))
for candidate in (".env.local", ".env"):
    path = os.path.join(base_dir, candidate)
    if os.path.exists(path):
        load_dotenv(path)
        break

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

import antibot

OK = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m!\033[0m"


def check_exit_ip(page) -> bool:
    """Descobre o IP de saída e se ele está classificado como datacenter."""
    print("\n── 1. IP de saída ─────────────────────────────────────────")
    try:
        page.goto(
            "http://ip-api.com/json/?fields=status,country,regionName,city,isp,org,as,mobile,proxy,hosting,query",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        data = json.loads(page.inner_text("pre, body").strip())
    except Exception as exc:
        print(f"  {FAIL} Não consegui consultar o IP: {exc}")
        print("      Se você configurou SCRAPER_PROXY, provavelmente ele está fora do ar")
        print("      ou as credenciais estão erradas.")
        return False

    if data.get("status") != "success":
        print(f"  {WARN} Consulta inconclusiva: {data}")
        return False

    print(f"  IP:    {data.get('query')}")
    print(f"  Local: {data.get('city')}, {data.get('regionName')} — {data.get('country')}")
    print(f"  ISP:   {data.get('isp')}")
    print(f"  ASN:   {data.get('as')}")

    hosting = data.get("hosting")
    mobile = data.get("mobile")

    if data.get("country") != "Brazil":
        print(f"  {WARN} IP fora do Brasil. ML e Amazon BR tratam tráfego estrangeiro")
        print("      com muito mais desconfiança. Prefira um proxy brasileiro.")

    if mobile:
        print(f"  {OK} Classificado como MÓVEL — a melhor reputação possível.")
    elif hosting:
        print(f"  {FAIL} Classificado como DATACENTER/HOSTING.")
        print("      É exatamente o que ML e Amazon bloqueiam na entrada.")
        if not antibot.get_proxy():
            print("      SCRAPER_PROXY está vazio — você está saindo pelo IP da própria máquina.")
        else:
            print("      Seu proxy NÃO é residencial de verdade. Troque de fornecedor.")
        return False
    else:
        print(f"  {OK} Classificado como residencial.")

    return True


def check_marketplace(page, nome: str, url: str, seletor: str) -> bool:
    print(f"\n── {nome} ─────────────────────────────────────────")
    response = None
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except PlaywrightTimeoutError:
        print(f"  {WARN} Timeout ao carregar. Pode ser lentidão do proxy.")

    reason = antibot.detect_block(page, response)
    if reason:
        print(f"  {FAIL} BLOQUEADO — {reason}")
        return False

    antibot.human_scroll(page)
    try:
        page.wait_for_selector(seletor, timeout=10000)
    except PlaywrightTimeoutError:
        print(f"  {WARN} Página carregou sem bloqueio, mas nenhum card apareceu.")
        print("      Provavelmente os seletores mudaram — não é problema de proxy.")
        return False

    total = len(page.query_selector_all(seletor))
    print(f"  {OK} Acesso liberado — {total} produtos visíveis na página.")
    return True


def main() -> int:
    proxy = antibot.get_proxy()
    print("=" * 60)
    print("  DIAGNÓSTICO DE ACESSO — Mercado Livre / Amazon")
    print("=" * 60)
    if proxy:
        # Nunca imprime usuário/senha.
        print(f"  Proxy configurado: {proxy['server']}")
        print(f"  Autenticado:       {'sim' if proxy.get('username') else 'não'}")
    else:
        print(f"  {WARN} SCRAPER_PROXY vazio — testando pelo IP direto desta máquina.")

    resultados = []

    with sync_playwright() as p:
        with antibot.browser_session(p, "diagnostico", headless=True) as page:
            print(f"  User-Agent: {page.evaluate('navigator.userAgent')}")
            print(f"  Plataforma: {page.evaluate('navigator.platform')}  "
                  f"(precisa ser coerente com o User-Agent acima)")

            resultados.append(("IP de saída", check_exit_ip(page)))
            antibot.human_pause(1.0, 2.0)

            resultados.append((
                "Mercado Livre",
                check_marketplace(
                    page, "2. Mercado Livre",
                    "https://lista.mercadolivre.com.br/fone-bluetooth",
                    "li.ui-search-layout__item, .poly-card",
                ),
            ))
            antibot.human_pause(2.0, 4.0)

            resultados.append((
                "Amazon",
                check_marketplace(
                    page, "3. Amazon Brasil",
                    "https://www.amazon.com.br/s?k=fone+bluetooth",
                    "div[data-component-type='s-search-result']",
                ),
            ))

    print("\n" + "=" * 60)
    print("  RESUMO")
    print("=" * 60)
    for nome, ok in resultados:
        print(f"  {OK if ok else FAIL} {nome}")

    marketplaces_ok = all(ok for nome, ok in resultados if nome != "IP de saída")
    if marketplaces_ok:
        # Mensagem neutra: este mesmo diagnóstico roda na VPS (via docker
        # compose exec) e no modo portátil, onde "subir na VPS" não faz sentido
        # nenhum — quem está com o pendrive na mão só quer saber se esta rede
        # serve.
        print("\n  Tudo liberado. Esta saída de rede serve para buscar ofertas.")
        return 0

    print("\n  Há bloqueio nesta saída de rede. Antes de mexer no código:")
    print("  - com proxy: confirme que ele é residencial brasileiro de verdade")
    print("    (o item 1 acima diz isso);")
    print("  - sem proxy: esta rede não serve (empresa, escola, VPS). Tente de")
    print("    uma rede doméstica.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
