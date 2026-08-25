"""
Busca na Shopee pela API oficial do Programa de Afiliados.

Por que esta rota existe: a Shopee decide pelo IP antes de olhar o navegador.
De rede institucional ou de datacenter ela troca a busca pela tela
`/verify/traffic/error` — e isso acontece igual com a janela visível, então não
é um problema de fingerprint que dê para contornar raspando melhor.

A API de afiliados não faz essa triagem: ela autentica por AppID + Secret e
responde de qualquer rede. De quebra devolve o `offerLink`, que já é o link
afiliado pronto — para um bot de divulgação isso resolve a busca e a
monetização na mesma chamada.

Onde ficam as credenciais (nesta ordem):
  1. SHOPEE_APP_ID / SHOPEE_APP_SECRET no .env.local do portal;
  2. os campos App Key / App Secret salvos na tela de configuração (o bot
     guarda no SQLite, e o portal lê de volta por /api/bot-settings).

Sem credencial nenhuma o scraper continua no caminho antigo, pelo navegador.
Elas saem em: Shopee Afiliados > Open API (https://affiliate.shopee.com.br).
"""

import hashlib
import json
import os
import sys
import time

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import requests
from rich.console import Console

import antibot

console = Console(force_terminal=True)

ENDPOINT = os.getenv("SHOPEE_API_URL", "https://open-api.affiliate.shopee.com.br/graphql")

# Campos pedidos ao productOfferV2. A lista reduzida é a rede de segurança:
# se a Shopee aposentar um campo, a consulta inteira falha com "Cannot query
# field", e sem esse plano B a loja sumiria do portal de um dia para o outro.
CAMPOS_COMPLETOS = (
    "itemId shopId productName priceMin priceMax priceDiscountRate "
    "imageUrl shopName productLink offerLink commissionRate sales ratingStar"
)
CAMPOS_MINIMOS = "itemId shopId productName priceMin imageUrl productLink offerLink"

# Cache das credenciais lidas do bot: a busca não pode bater no bot a cada
# página, e essas chaves quase nunca mudam.
_cache_credenciais = {"valor": None, "lido_em": 0.0}
_TTL_CREDENCIAIS = 300.0


def _do_ambiente() -> tuple[str, str]:
    return (
        (os.getenv("SHOPEE_APP_ID") or "").strip(),
        (os.getenv("SHOPEE_APP_SECRET") or "").strip(),
    )


def _do_bot() -> tuple[str, str]:
    """Lê App Key/Secret salvos pela tela de configuração (banco do bot)."""
    base = (os.getenv("BOT_API_URL") or "").rstrip("/")
    if not base:
        return "", ""

    cabecalhos = {}
    token = (os.getenv("BOT_API_TOKEN") or "").strip()
    if token:
        cabecalhos["X-Api-Token"] = token

    try:
        resposta = requests.get(f"{base}/api/bot-settings", headers=cabecalhos, timeout=5)
        dados = resposta.json() if resposta.status_code == 200 else {}
    except Exception:
        return "", ""

    config = (dados or {}).get("settings") or {}
    return (
        str(config.get("shopeeAppKey") or "").strip(),
        str(config.get("shopeeAppSecret") or "").strip(),
    )


def credenciais(forcar: bool = False) -> tuple[str, str]:
    """AppID e Secret em uso, com cache curto."""
    agora = time.time()
    if not forcar and _cache_credenciais["valor"] and (agora - _cache_credenciais["lido_em"]) < _TTL_CREDENCIAIS:
        return _cache_credenciais["valor"]

    par = _do_ambiente()
    if not all(par):
        par = _do_bot()

    _cache_credenciais["valor"] = par
    _cache_credenciais["lido_em"] = agora
    return par


class ShopeeAffiliateAPI:
    """Cliente mínimo do GraphQL de afiliados da Shopee."""

    def __init__(self, app_id: str | None = None, app_secret: str | None = None):
        if app_id is None or app_secret is None:
            app_id, app_secret = credenciais()
        self.app_id = app_id or ""
        self.app_secret = app_secret or ""
        self.last_error: str | None = None

    def configurada(self) -> bool:
        return bool(self.app_id and self.app_secret)

    # ------------------------------------------------------------------
    def _assinatura(self, corpo: str, instante: int) -> str:
        """
        SHA256(AppId + Timestamp + Payload + Secret), em hexadecimal.

        O `corpo` precisa ser exatamente a mesma string enviada na requisição —
        reserializar o JSON depois de assinar muda um espaço e derruba a
        assinatura com "Invalid Signature".
        """
        bruto = f"{self.app_id}{instante}{corpo}{self.app_secret}"
        return hashlib.sha256(bruto.encode("utf-8")).hexdigest()

    def _consultar(self, query: str) -> tuple[dict | None, str | None]:
        corpo = json.dumps({"query": query}, separators=(",", ":"), ensure_ascii=False)
        instante = int(time.time())
        assinatura = self._assinatura(corpo, instante)

        cabecalhos = {
            "Content-Type": "application/json",
            "Authorization": (
                f"SHA256 Credential={self.app_id}, "
                f"Timestamp={instante}, Signature={assinatura}"
            ),
        }

        try:
            resposta = requests.post(
                ENDPOINT,
                data=corpo.encode("utf-8"),
                headers=cabecalhos,
                proxies=antibot.http_proxies(),
                timeout=25,
            )
        except Exception as exc:
            return None, f"Shopee (API de afiliados): falha de conexão ({exc})."

        if resposta.status_code != 200:
            return None, f"Shopee (API de afiliados): HTTP {resposta.status_code} — {resposta.text[:160]}"

        try:
            dados = resposta.json()
        except ValueError:
            return None, f"Shopee (API de afiliados): resposta ilegível — {resposta.text[:160]}"

        erros = dados.get("errors") or []
        if erros:
            return dados, self._descrever_erro(erros)

        return dados, None

    @staticmethod
    def _descrever_erro(erros: list) -> str:
        partes = []
        for erro in erros[:3]:
            if isinstance(erro, dict):
                codigo = erro.get("code") or (erro.get("extensions") or {}).get("code")
                mensagem = erro.get("message") or erro.get("msg") or str(erro)
                partes.append(f"{mensagem}{f' (código {codigo})' if codigo else ''}")
            else:
                partes.append(str(erro))

        detalhe = " | ".join(partes)
        dica = ""
        alvo = detalhe.lower()
        if "signature" in alvo:
            dica = " Confira se o App Secret está correto e se o relógio da máquina está na hora certa."
        elif "permission" in alvo or "not authorized" in alvo or "10035" in alvo:
            dica = " A conta precisa ter a Open API liberada no painel de afiliados da Shopee."
        elif "credential" in alvo or "app id" in alvo:
            dica = " Confira o App ID."

        return f"Shopee (API de afiliados): {detalhe}.{dica}"

    # ------------------------------------------------------------------
    def buscar_produtos(self, keyword: str, pagina: int = 1, limite: int = 50) -> tuple[list[dict], str | None]:
        """Devolve (nós crus da API, erro). Lista vazia com erro None = sem resultados."""
        self.last_error = None

        if not self.configurada():
            return [], "Shopee: App ID/Secret da API de afiliados não configurados."

        # O limite da API é 50 por página.
        limite = max(1, min(int(limite), 50))
        termo = json.dumps(keyword.strip(), ensure_ascii=False)  # já sai entre aspas e escapado

        for campos in (CAMPOS_COMPLETOS, CAMPOS_MINIMOS):
            query = (
                "{ productOfferV2("
                f"keyword: {termo}, page: {max(1, int(pagina))}, limit: {limite}"
                ") { nodes { " + campos + " } } }"
            )

            dados, erro = self._consultar(query)

            if erro:
                # Campo aposentado: vale repetir com a lista curta antes de desistir.
                if "cannot query field" in erro.lower() and campos is CAMPOS_COMPLETOS:
                    console.print("[dim]Shopee: campo recusado pela API, repetindo com a consulta reduzida...[/dim]")
                    continue
                self.last_error = erro
                return [], erro

            nos = (((dados or {}).get("data") or {}).get("productOfferV2") or {}).get("nodes")
            return (nos if isinstance(nos, list) else []), None

        return [], self.last_error
