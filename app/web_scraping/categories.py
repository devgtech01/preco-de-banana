"""
Catálogo e mecanismo de busca por Categorias com foco prioritário em Tecnologia.

Suporta todos os marketplaces registrados no sistema:
- Mercado Livre
- Amazon Brasil
- Shopee
- AliExpress
- KaBuM!
"""

import os
import re
import sys
import time
from typing import Optional

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

import pandas as pd
from rich.console import Console

import marketplaces
from utils import save_to_csv, save_to_json

console = Console(force_terminal=True)

# ---------------------------------------------------------------------------
# Catálogo de Categorias (com prioridade em Tecnologia e Eletrônicos)
# ---------------------------------------------------------------------------
CATEGORIAS: dict[str, dict] = {
    "tecnologia": {
        "nome": "Tecnologia & Informática (Geral)",
        "emoji": "💻",
        "prioridade": 1,
        "descricao": "Principais produtos do ecossistema de tecnologia e computação",
        "termos": [
            "notebook i7",
            "smartphone 5g",
            "placa de video rtx",
            "monitor gamer 144hz",
            "ssd nvme 1tb",
            "smart tv 4k",
            "fone bluetooth",
            "smartwatch"
        ]
    },
    "notebooks": {
        "nome": "Notebooks & Computadores",
        "emoji": "💻",
        "prioridade": 2,
        "descricao": "Laptops para trabalho, estudo e notebooks gamers de alta performance",
        "termos": [
            "notebook i7",
            "notebook gamer rtx",
            "notebook i5 16gb",
            "macbook air m2",
            "pc gamer completo"
        ]
    },
    "smartphones": {
        "nome": "Smartphones & Celulares",
        "emoji": "📱",
        "prioridade": 3,
        "descricao": "Celulares topo de linha, intermediários e lançamentos 5G",
        "termos": [
            "iphone 15",
            "samsung galaxy s24",
            "xiaomi redmi note 13",
            "motorola edge",
            "smartphone 5g 256gb"
        ]
    },
    "hardware": {
        "nome": "Hardware & Componentes PC",
        "emoji": "⚡",
        "prioridade": 4,
        "descricao": "Placas de vídeo, processadores, memórias e armazenamento",
        "termos": [
            "placa de video rtx 4060",
            "processador ryzen 7",
            "ssd nvme 1tb",
            "memoria ram ddr5 32gb",
            "placa mae b550",
            "fonte 650w plus bronze"
        ]
    },
    "perifericos": {
        "nome": "Periféricos & Monitores",
        "emoji": "⌨️",
        "prioridade": 5,
        "descricao": "Monitores gamer, teclados mecânicos, mouses e headsets",
        "termos": [
            "monitor 144hz 1ms ips",
            "teclado mecanico rgb",
            "mouse gamer sem fio",
            "headset gamer 7.1",
            "microfone condensador usb"
        ]
    },
    "games": {
        "nome": "Games & Consoles",
        "emoji": "🎮",
        "prioridade": 6,
        "descricao": "Consoles da nova geração, controles, acessórios e jogos",
        "termos": [
            "playstation 5 slim",
            "nintendo switch oled",
            "xbox series s",
            "controle ps5 dualsense",
            "volante gamer g29"
        ]
    },
    "audio": {
        "nome": "Áudio & Som",
        "emoji": "🎧",
        "prioridade": 7,
        "descricao": "Fones bluetooth com cancelamento de ruído, caixas de som e soundbars",
        "termos": [
            "fone bluetooth anc",
            "caixa de som jbl bluetooth",
            "soundbar 2.1",
            "fone tws sem fio",
            "headphone bluetooth"
        ]
    },
    "smart_home": {
        "nome": "Smart Home & Casa Inteligente",
        "emoji": "🏠",
        "prioridade": 8,
        "descricao": "Dispositivos conectados, assistentes virtuais e automação residencial",
        "termos": [
            "alexa echo dot",
            "lampada inteligente wifi",
            "camera seguranca wifi",
            "fechadura digital biometrica",
            "robo aspirador wifi"
        ]
    },
    "wearables": {
        "nome": "Smartwatches & Wearables",
        "emoji": "⌚",
        "prioridade": 9,
        "descricao": "Relógios inteligentes, smartbands e monitores de saúde",
        "termos": [
            "apple watch",
            "samsung galaxy watch",
            "smartwatch amazfit",
            "smartband xiaomi band"
        ]
    },
    "tablets": {
        "nome": "Tablets & E-readers",
        "emoji": "📲",
        "prioridade": 10,
        "descricao": "Tablets para produtividade e estudos, canetas stylus e Kindle",
        "termos": [
            "ipad 10 geracao",
            "tablet samsung galaxy tab",
            "kindle paperwhite 16gb",
            "tablet lenovo"
        ]
    },
    "tv_video": {
        "nome": "TV & Vídeo",
        "emoji": "📺",
        "prioridade": 11,
        "descricao": "Smart TVs 4K, projetores e receptores de streaming",
        "termos": [
            "smart tv 50 polegadas 4k",
            "smart tv 55 oled",
            "fire tv stick 4k",
            "projetor smart 4k"
        ]
    }
}


def listar_categorias() -> list[dict]:
    """Retorna a lista estruturada de categorias para APIs e frontend."""
    lista = []
    for chave, info in sorted(CATEGORIAS.items(), key=lambda x: x[1].get("prioridade", 99)):
        lista.append({
            "chave": chave,
            "nome": info["nome"],
            "emoji": info["emoji"],
            "prioridade": info.get("prioridade", 99),
            "descricao": info.get("descricao", ""),
            "termos": info.get("termos", []),
            "total_termos": len(info.get("termos", []))
        })
    return lista


def padronizar_produto(item: dict, categoria_nome: str = "Tecnologia", termo_pesquisado: str = "") -> dict:
    """
    Padroniza os campos do produto garantindo nome_produto, link, valor_antes, valor_depois
    e mantendo compatibilidade com os campos históricos (titulo, preco, preco_original).
    """
    preco_atual = item.get("preco")
    preco_antigo = item.get("preco_original")

    # Calcula percentual de desconto se houver preço antes e depois
    desconto = item.get("destaque")
    if not desconto and preco_antigo and preco_atual and preco_antigo > preco_atual:
        pct = round(((preco_antigo - preco_atual) / preco_antigo) * 100)
        desconto = f"{pct}% OFF"

    return {
        # Campos canônicos pedidos pelo usuário:
        "nome_produto": item.get("titulo") or "",
        "link": item.get("link") or "",
        "valor_antes": preco_antigo,
        "valor_depois": preco_atual,
        "desconto": desconto,
        "categoria": categoria_nome,
        "termo_pesquisado": termo_pesquisado,
        "plataforma": item.get("plataforma") or "Desconhecida",
        "frete_gratis": bool(item.get("frete_gratis")),
        "cupom": item.get("cupom"),
        "vendedor": item.get("vendedor") or "",
        "condicao": item.get("condicao") or "Novo",
        "imagem_url": item.get("imagem_url"),
        # Campos para retrocompatibilidade com a tabela e bot:
        "titulo": item.get("titulo") or "",
        "preco": preco_atual,
        "preco_original": preco_antigo,
        "destaque": desconto,
    }


def buscar_produtos_por_categoria(
    categoria_chave: str,
    plataformas: str | list[str] = "all",
    limite_por_termo: int = 10,
    max_paginas: int = 1,
    limite_total: int = 50,
    headless: bool = True
) -> list[dict]:
    """
    Realiza a busca dos principais produtos de uma categoria em cada marketplace especificado.
    """
    cat_info = CATEGORIAS.get(categoria_chave.lower())
    if not cat_info:
        # Tenta casar por substring ou cai no padrão 'tecnologia'
        for k, v in CATEGORIAS.items():
            if categoria_chave.lower() in k or categoria_chave.lower() in v["nome"].lower():
                cat_info = v
                categoria_chave = k
                break
        if not cat_info:
            cat_info = CATEGORIAS["tecnologia"]
            categoria_chave = "tecnologia"

    categoria_nome = cat_info["nome"]
    termos = cat_info.get("termos", ["notebook i7", "smartphone 5g"])
    chaves_lojas = marketplaces.resolver_plataformas(plataformas)

    console.print(
        f"\n[bold cyan]🔍 Buscando Categoria:[/bold cyan] [bold yellow]{cat_info['emoji']} {categoria_nome}[/bold yellow]"
    )
    console.print(
        f"[dim]Lojas: {marketplaces.descricao(chaves_lojas)} | Termos: {len(termos)} | Limite: {limite_total}[/dim]\n"
    )

    todos_produtos: list[dict] = []
    links_vistos: set[str] = set()

    for termo in termos:
        if len(todos_produtos) >= limite_total:
            break

        cota = marketplaces.cota_por_loja(limite_por_termo, len(chaves_lojas))
        por_loja = []

        for chave in chaves_lojas:
            try:
                scraper = marketplaces.criar_scraper(chave, headless=headless)
                itens_loja = scraper.search_products(query=termo, max_pages=max_paginas, max_items=cota)
                por_loja.append(itens_loja)
            except Exception as exc:
                console.print(f"[red]Erro ao buscar '{termo}' em {chave}: {exc}[/red]")
                por_loja.append([])

        itens_combinados = marketplaces.intercalar(por_loja)

        for item in itens_combinados:
            link = item.get("link")
            if not link or link in links_vistos:
                continue
            links_vistos.add(link)

            prod_padrao = padronizar_produto(item, categoria_nome=categoria_nome, termo_pesquisado=termo)
            todos_produtos.append(prod_padrao)

            if len(todos_produtos) >= limite_total:
                break

    return todos_produtos[:limite_total]


def exportar_produtos(
    produtos: list[dict],
    nome_base: str = "produtos_categoria",
    formato: str = "all",
    pasta_destino: str = "data"
) -> dict[str, str]:
    """
    Exporta a lista de produtos padronizados para CSV e JSON no disco.
    Retorna os caminhos dos arquivos gerados.
    """
    os.makedirs(pasta_destino, exist_ok=True)
    caminhos = {}

    nome_sanitizado = re.sub(r'[^\w\s-]', '', nome_base.lower().strip())
    nome_sanitizado = re.sub(r'[-\s]+', '_', nome_sanitizado)
    timestamp = time.strftime("%Y%m%d_%H%M%S")

    # Ordenação das colunas para melhor leitura no CSV/JSON
    colunas_preferidas = [
        "nome_produto",
        "link",
        "valor_antes",
        "valor_depois",
        "desconto",
        "categoria",
        "termo_pesquisado",
        "plataforma",
        "frete_gratis",
        "cupom",
        "vendedor",
        "condicao",
        "imagem_url"
    ]

    # Prepara lista de dicionários limpa para exportação
    dados_export = []
    for p in produtos:
        item_ordenado = {}
        for col in colunas_preferidas:
            item_ordenado[col] = p.get(col)
        # Inclui quaisquer outras chaves extras
        for k, v in p.items():
            if k not in item_ordenado and k not in ("titulo", "preco", "preco_original", "destaque"):
                item_ordenado[k] = v
        dados_export.append(item_ordenado)

    if formato in ("json", "all"):
        json_path = os.path.join(pasta_destino, f"{nome_sanitizado}_{timestamp}.json")
        save_to_json(dados_export, json_path)
        caminhos["json"] = json_path

    if formato in ("csv", "all"):
        csv_path = os.path.join(pasta_destino, f"{nome_sanitizado}_{timestamp}.csv")
        if dados_export:
            df = pd.DataFrame(dados_export)
            # Reorganiza colunas existentes conforme preferência
            cols = [c for c in colunas_preferidas if c in df.columns] + [c for c in df.columns if c not in colunas_preferidas]
            df = df[cols]
            df.to_csv(csv_path, index=False, encoding='utf-8-sig')
            console.print(f"[bold green]✓[/bold green] CSV exportado: [cyan]{csv_path}[/cyan]")
        else:
            save_to_csv([], csv_path)
        caminhos["csv"] = csv_path

    return caminhos
