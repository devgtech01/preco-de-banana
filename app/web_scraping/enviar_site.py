"""
Envio das ofertas coletadas para o site (Tech Wolf).

O coletor roda numa rede de casa porque as lojas barram IP de servidor. O site
fica num servidor. Por isso quem inicia a conversa é sempre este lado: o site
nunca precisa alcançar este computador, que continua fechado para a internet.

O site recebe as ofertas em /admin/oportunidades, aplica o link de afiliado de
cada loja e, conforme as regras configuradas lá, coloca no catálogo.

Configuração (config.txt do pendrive ou app/web_scraping/.env.local):
    SITE_API_URL    endereço do site, ex.: https://techofertas.com.br
    SITE_API_TOKEN  o mesmo INTEGRATION_API_TOKEN configurado no site
    SITE_ENVIO_AUTOMATICO=1  envia sozinho depois de cada busca no portal
"""

import os

import requests

ROTA = "/api/integracao/oportunidades"
# O site aceita até 500 por requisição; lotes menores falham menos em rede ruim.
TAMANHO_DO_LOTE = 200

# Campos que o site entende. O resto (estatísticas da tela, flags internas) fica aqui.
CAMPOS = (
    "nome_produto", "titulo", "link", "valor_antes", "preco_original", "valor_depois", "preco",
    "categoria", "termo_pesquisado", "plataforma", "frete_gratis", "cupom", "vendedor", "imagem_url",
)


def _url() -> str:
    return (os.getenv("SITE_API_URL") or "").strip().rstrip("/")


def _token() -> str:
    return (os.getenv("SITE_API_TOKEN") or "").strip()


def configurado() -> bool:
    return bool(_url() and _token())


def envio_automatico() -> bool:
    return configurado() and (os.getenv("SITE_ENVIO_AUTOMATICO") or "").strip().lower() in ("1", "true", "sim", "yes")


def _cabecalhos() -> dict:
    return {"Authorization": f"Bearer {_token()}", "User-Agent": "preco-de-banana/coletor"}


def _limpar(produto: dict) -> dict:
    return {campo: produto.get(campo) for campo in CAMPOS if produto.get(campo) not in (None, "")}


def _erro_da_resposta(resp: requests.Response) -> str:
    try:
        return resp.json().get("erro") or f"HTTP {resp.status_code}"
    except ValueError:
        return f"HTTP {resp.status_code}"


def testar_conexao() -> dict:
    """Confere endereço e token sem enviar nada."""
    if not configurado():
        return {"ok": False, "erro": "Preencha SITE_URL e SITE_TOKEN no config.txt."}
    try:
        resp = requests.get(_url() + ROTA, headers=_cabecalhos(), timeout=15)
        if resp.status_code == 200:
            return {"ok": True}
        return {"ok": False, "erro": _erro_da_resposta(resp)}
    except requests.RequestException as erro:
        return {"ok": False, "erro": f"Site não respondeu: {erro.__class__.__name__}"}


def enviar_oportunidades(produtos: list[dict], origem: str = "portal") -> dict:
    """
    Envia as ofertas em lotes. Devolve o resumo somado do site:
    novas, atualizadas, aprovadas automaticamente, inválidas e erros.
    """
    resumo = {
        "ok": False,
        "enviadas": 0,
        "novas": 0,
        "atualizadas": 0,
        "aprovadasAutomaticamente": 0,
        "produtosAtualizados": 0,
        "invalidas": 0,
        "erros": [],
    }
    if not configurado():
        resumo["erros"].append("Envio para o site desligado: preencha SITE_URL e SITE_TOKEN no config.txt.")
        return resumo

    itens = [_limpar(p) for p in produtos if p.get("link")]
    for inicio in range(0, len(itens), TAMANHO_DO_LOTE):
        lote = itens[inicio:inicio + TAMANHO_DO_LOTE]
        try:
            resp = requests.post(
                _url() + ROTA,
                json={"origem": origem, "itens": lote},
                headers=_cabecalhos(),
                timeout=60,
            )
        except requests.RequestException as erro:
            resumo["erros"].append(f"Site não respondeu: {erro.__class__.__name__}")
            break

        if resp.status_code != 200:
            resumo["erros"].append(_erro_da_resposta(resp))
            # Token errado ou integração desligada: os próximos lotes falhariam igual.
            if resp.status_code in (401, 503):
                break
            continue

        dados = resp.json()
        resumo["enviadas"] += len(lote)
        for campo in ("novas", "atualizadas", "aprovadasAutomaticamente", "produtosAtualizados", "invalidas"):
            resumo[campo] += int(dados.get(campo) or 0)

    resumo["ok"] = resumo["enviadas"] > 0 and not resumo["erros"]
    return resumo


def descrever(resumo: dict) -> str:
    """Frase curta para o log e para a tela."""
    if not resumo.get("enviadas"):
        return "Site: " + ("; ".join(resumo.get("erros") or []) or "nada enviado.")
    partes = [f"{resumo['novas']} nova(s)", f"{resumo['atualizadas']} atualizada(s)"]
    if resumo.get("aprovadasAutomaticamente"):
        partes.append(f"{resumo['aprovadasAutomaticamente']} já no catálogo")
    if resumo.get("produtosAtualizados"):
        partes.append(f"{resumo['produtosAtualizados']} preço(s) atualizado(s)")
    texto = "Site: " + ", ".join(partes) + "."
    if resumo.get("erros"):
        texto += " Avisos: " + "; ".join(resumo["erros"])
    return texto
