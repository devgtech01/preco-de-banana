"""
Camada de evasão anti-bot compartilhada pelos scrapers (Mercado Livre / Amazon).

Concentra tudo que decide se a requisição "parece humana":
  - fingerprint coerente com o SO real onde o Firefox está rodando
  - cabeçalhos e preferências de navegador realistas
  - suporte a proxy (residencial) via variável de ambiente
  - perfil persistente (cookies sobrevivem entre buscas)
  - detecção explícita de bloqueio/CAPTCHA
  - ritmo humano (jitter) e backoff exponencial

Motivo de existir: o código antigo anunciava um User-Agent de Windows enquanto o
Firefox rodava em Linux dentro do container. `navigator.platform` continua
retornando "Linux x86_64", então o par UA x plataforma ficava inconsistente -
o teste mais barato que qualquer antibot faz. Localmente (Windows) o par batia,
e por isso a mesma busca funcionava na máquina do dev e falhava na VPS.
"""

import os
import random
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

TIMEZONE = os.getenv("SCRAPER_TIMEZONE", "America/Sao_Paulo")
LOCALE = "pt-BR"

# Perfil de tela plausível para desktop (evita o 1440x900 exato repetido sempre).
VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1680, "height": 1050},
    {"width": 1600, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
]

# Trechos que indicam que a página devolvida NÃO é o resultado de busca,
# e sim um desafio de verificação. Ambos os sites respondem HTTP 200 nesses
# casos, então checar o status code não basta.
BLOCK_MARKERS = (
    # Amazon
    "digite os caracteres que voc",
    "enter the characters you see",
    "type the characters you see",
    "validatecaptcha",
    "api-services-support@amazon.com",
    "sorry, we just need to make sure you're not a robot",
    "para discutir o acesso automatizado",
    # Mercado Livre
    "verifica&ccedil;&atilde;o de seguran",
    "verificação de segurança",
    "para continuar, confirme que voc",
    "acesso negado",
    "/gz/security",
    # Genéricos / WAF
    "just a moment...",
    "checking your browser before accessing",
    "access denied",
    "request blocked",
    "unusual traffic",
    # Shopee / AliExpress
    "verifique se você é humano",
    "punish/captcha",
    "_bx-v",
)

# "captcha" solto NÃO entra em BLOCK_MARKERS: a palavra aparece em tags de
# script de páginas legítimas e derrubaria buscas boas por falso positivo.
BLOCK_URL_MARKERS = ("captcha", "verification", "/errors/", "challenge", "blocked", "/verify/")


class BlockedError(RuntimeError):
    """Levantada quando o marketplace respondeu com desafio/CAPTCHA em vez de resultados."""


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "sim")


def get_proxy() -> dict | None:
    """
    Lê o proxy de saída de SCRAPER_PROXY.

    Formato aceito: http://usuario:senha@host:porta (ou socks5://...).
    Sem essa variável o scraper sai pelo IP da própria máquina - o que na VPS
    significa um IP de datacenter, bloqueado na origem por ML e Amazon.
    """
    raw = (os.getenv("SCRAPER_PROXY") or "").strip()
    if not raw:
        return None

    match = re.match(r"^(?P<scheme>https?|socks5)://(?:(?P<user>[^:@/]+):(?P<pwd>[^@/]*)@)?(?P<host>[^@/]+)$", raw)
    if not match:
        print(f"[antibot] SCRAPER_PROXY ignorado: formato inválido ({raw[:40]})", flush=True)
        return None

    proxy = {"server": f"{match.group('scheme')}://{match.group('host')}"}
    if match.group("user"):
        proxy["username"] = match.group("user")
        proxy["password"] = match.group("pwd") or ""
    return proxy


def default_user_agent(version: str = "134.0") -> str:
    """
    User-Agent coerente com o SO real, sem depender de um navegador aberto.

    Serve tanto para o Playwright quanto para o caminho HTTP simples (requests),
    usado pelas lojas que entregam o catálogo sem precisar de JavaScript.
    """
    override = (os.getenv("SCRAPER_USER_AGENT") or "").strip()
    if override:
        return override

    major = (version or "134.0").split(".")[0]

    if sys.platform.startswith("win"):
        platform_token = "Windows NT 10.0; Win64; x64"
    elif sys.platform == "darwin":
        platform_token = "Macintosh; Intel Mac OS X 10.15"
    else:
        platform_token = "X11; Linux x86_64"

    return f"Mozilla/5.0 ({platform_token}; rv:{major}.0) Gecko/20100101 Firefox/{major}.0"


def user_agent_for(browser) -> str:
    """
    Monta um User-Agent coerente com o SO e a versão real do Firefox em uso.

    Permite override por SCRAPER_USER_AGENT, mas o padrão passa a ser sempre
    consistente com o que o próprio navegador expõe em navigator.platform.
    """
    return default_user_agent(getattr(browser, "version", None) or "134.0")


def firefox_prefs() -> dict:
    """Preferências do Firefox que removem sinais óbvios de automação."""
    return {
        # Esconde navigator.webdriver (o Playwright deixa true por padrão).
        "dom.webdriver.enabled": False,
        "useAutomationExtension": False,
        # Impede vazamento do IP real por WebRTC quando um proxy está em uso.
        "media.peerconnection.enabled": False,
        # Idioma e região coerentes com o Brasil.
        "intl.accept_languages": "pt-BR,pt,en-US,en",
        "browser.search.region": "BR",
        # Comportamento de navegador comum (não bloquear cookies de terceiros).
        "network.cookie.cookieBehavior": 0,
        "privacy.trackingprotection.enabled": False,
        "privacy.resistFingerprinting": False,
        # Evita telas de "primeira execução" que atrapalham o load.
        "browser.shell.checkDefaultBrowser": False,
        "datareporting.healthreport.uploadEnabled": False,
    }


def context_options(browser) -> dict:
    """Opções do BrowserContext com fingerprint e cabeçalhos realistas."""
    options = {
        "user_agent": user_agent_for(browser),
        "viewport": random.choice(VIEWPORTS),
        "locale": LOCALE,
        "timezone_id": TIMEZONE,
        "geolocation": {"latitude": -23.5505, "longitude": -46.6333},  # São Paulo
        "permissions": ["geolocation"],
        "extra_http_headers": {
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "DNT": "1",
        },
    }

    proxy = get_proxy()
    if proxy:
        options["proxy"] = proxy

    return options


INIT_SCRIPT = """
// Remove os resíduos de automação que sobrevivem às prefs do Firefox.
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
if (!navigator.languages || navigator.languages.length === 0) {
    Object.defineProperty(navigator, 'languages', { get: () => ['pt-BR', 'pt', 'en-US', 'en'] });
}
// Um desktop real quase nunca reporta 0 núcleos ou 0 GB.
if (!navigator.hardwareConcurrency) {
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
}
"""


def prepare_context(context) -> None:
    """Aplica o script de inicialização e a política de bloqueio de recursos."""
    context.add_init_script(INIT_SCRIPT)

    # Modo econômico: corta imagens e fontes para gastar menos banda de proxy.
    # Fica DESLIGADO por padrão porque bloquear fontes/imagens é, em si, um
    # padrão de requisição anômalo que alguns antibots pontuam.
    light_mode = _env_flag("SCRAPER_LIGHT_MODE", False)
    blocked_types = {"media"}
    if light_mode:
        blocked_types |= {"image", "font"}

    def _route(route):
        try:
            if route.request.resource_type in blocked_types:
                route.abort()
            else:
                route.continue_()
        except Exception:
            pass

    context.route("**/*", _route)


def human_pause(minimum: float = 0.6, maximum: float = 1.8) -> None:
    """Pausa com duração variável - tempos exatos e repetidos denunciam automação."""
    time.sleep(random.uniform(minimum, maximum))


def human_scroll(page) -> None:
    """Rola a página em passos irregulares, como um usuário lendo os resultados."""
    try:
        steps = random.randint(3, 5)
        for _ in range(steps):
            page.mouse.wheel(0, random.randint(400, 900))
            time.sleep(random.uniform(0.25, 0.7))
        # Pequeno movimento de mouse: gera eventos que o antibot procura.
        page.mouse.move(random.randint(200, 900), random.randint(200, 700))
    except Exception:
        pass


def detect_block(page, response=None) -> str | None:
    """
    Retorna o motivo do bloqueio, ou None se a página parece legítima.

    Checa três camadas porque nenhuma isolada é confiável:
      1. status HTTP (403/429/503)
      2. URL final (redirecionamento para captcha/erro)
      3. conteúdo (os dois sites devolvem desafio com HTTP 200)
    """
    if response is not None:
        try:
            status = response.status
            if status in (403, 429, 503):
                return f"HTTP {status}"
        except Exception:
            pass

    try:
        url = (page.url or "").lower()
        if any(marker in url for marker in BLOCK_URL_MARKERS):
            return f"URL de verificação ({page.url[:80]})"
    except Exception:
        pass

    try:
        # Só o começo do HTML: o desafio sempre vem no topo e evita ler 2MB à toa.
        html = page.content()[:20000].lower()
        for marker in BLOCK_MARKERS:
            if marker in html:
                return f"CAPTCHA/desafio detectado ('{marker[:35]}')"
    except Exception:
        pass

    return None


def backoff_sleep(attempt: int) -> None:
    """Espera exponencial com ruído antes de repetir uma tentativa bloqueada."""
    delay = min(60.0, (2 ** attempt) * random.uniform(2.0, 4.0))
    print(f"[antibot] Aguardando {delay:.1f}s antes da próxima tentativa...", flush=True)
    time.sleep(delay)


def storage_state_path(name: str) -> Path | None:
    """
    Arquivo de sessão (cookies + localStorage) por marketplace.

    Manter a sessão entre buscas é o que diferencia um visitante recorrente de
    um bot que chega sempre "virgem". Desligue com SCRAPER_PERSIST_PROFILE=0.
    """
    if not _env_flag("SCRAPER_PERSIST_PROFILE", True):
        return None

    base = os.getenv("SCRAPER_PROFILE_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "profiles"
    )
    directory = Path(base)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{name}.json"


def max_attempts() -> int:
    try:
        return max(1, int(os.getenv("SCRAPER_MAX_ATTEMPTS", "3")))
    except ValueError:
        return 3


@contextmanager
def browser_session(playwright, profile_name: str, headless: bool = True):
    """
    Abre um Firefox mascarado e devolve a página pronta para navegar.

    Carrega os cookies da execução anterior e os grava de volta ao final, para
    que o marketplace veja um visitante recorrente em vez de uma sessão nova a
    cada busca.
    """
    browser = playwright.firefox.launch(
        headless=headless,
        firefox_user_prefs=firefox_prefs(),
    )

    options = context_options(browser)

    state_file = storage_state_path(profile_name)
    if state_file and state_file.exists():
        options["storage_state"] = str(state_file)

    context = browser.new_context(**options)
    prepare_context(context)

    page = context.new_page()
    try:
        yield page
    finally:
        if state_file:
            try:
                context.storage_state(path=str(state_file))
            except Exception as exc:
                print(f"[antibot] Não foi possível salvar a sessão: {exc}", flush=True)

        # O Firefox do Playwright ocasionalmente devolve um erro de protocolo ao
        # fechar o contexto ("_maybeDontRestoreTabs"). É ruído de desligamento e
        # não pode derrubar uma raspagem que já terminou com sucesso.
        try:
            context.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Caminho HTTP simples (sem Playwright)
#
# KaBuM! e AliExpress entregam o catálogo inteiro embutido no HTML da busca,
# então uma requisição comum resolve. Abrir o Firefox custa de 5 a 15 segundos
# (bem mais no modo pendrive, lendo de uma porta USB), e cada navegador aberto
# é memória que a máquina do usuário precisa ter. Shopee não entra aqui: a API
# de busca dela só responde com os cookies que o próprio site emite.
# ---------------------------------------------------------------------------

def http_first_enabled() -> bool:
    """Se falso, todas as lojas passam direto pelo navegador."""
    return _env_flag("SCRAPER_HTTP_FIRST", True)


def http_headers(extra: dict | None = None) -> dict:
    """Cabeçalhos de navegador para o caminho sem Playwright."""
    headers = {
        "User-Agent": default_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "DNT": "1",
        "Connection": "keep-alive",
    }
    if extra:
        headers.update(extra)
    return headers


def http_proxies() -> dict | None:
    """Mesmo SCRAPER_PROXY do Playwright, no formato que o requests espera."""
    raw = (os.getenv("SCRAPER_PROXY") or "").strip()
    if not raw:
        return None
    return {"http": raw, "https": raw}


def detect_block_text(html: str, status: int | None = None) -> str | None:
    """
    Versão de detect_block para quem só tem o HTML em mãos (requests).

    Mesma lógica das duas primeiras camadas: status HTTP e conteúdo. A checagem
    de URL final fica de fora porque o requests já segue os redirecionamentos.
    """
    if status in (403, 429, 503):
        return f"HTTP {status}"

    trecho = (html or "")[:20000].lower()
    for marker in BLOCK_MARKERS:
        if marker in trecho:
            return f"CAPTCHA/desafio detectado ('{marker[:35]}')"

    return None
