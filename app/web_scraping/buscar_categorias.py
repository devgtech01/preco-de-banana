"""
Script CLI para buscar os principais produtos por categoria (Foco em Tecnologia)
em múltiplos marketplaces (Mercado Livre, Amazon, Shopee, AliExpress, KaBuM!)
e exportar os resultados diretamente para CSV e JSON.

Uso:
  python buscar_categorias.py --categoria tecnologia --lojas all --limite 30
  python buscar_categorias.py --categoria hardware --lojas kabum,amazon
  python buscar_categorias.py --todas-categorias
  python buscar_categorias.py --listar
"""

import argparse
import os
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

from rich.console import Console
from rich.table import Table

import categories
from utils import display_summary_table

console = Console(force_terminal=True)


def exibir_tabela_categorias():
    """Mostra as categorias disponíveis no terminal."""
    lista = categories.listar_categorias()
    table = Table(title="🏷️ Categorias Disponíveis para Busca", header_style="bold magenta", show_lines=True)
    table.add_column("Chave", style="cyan", width=14)
    table.add_column("Nome da Categoria", style="bold white", width=30)
    table.add_column("Termos de Busca Monitorados", style="green")

    for cat in lista:
        termos_preview = ", ".join(cat["termos"][:3]) + (f" (+{len(cat['termos'])-3})" if len(cat["termos"]) > 3 else "")
        table.add_row(cat["chave"], f"{cat['emoji']} {cat['nome']}", termos_preview)

    console.print(table)


def main():
    parser = argparse.ArgumentParser(
        description="Busca de produtos por Categoria com prioridade em Tecnologia nos Marketplaces (ML, Amazon, Shopee, AliExpress, KaBuM!)."
    )
    parser.add_argument(
        "-c", "--categoria",
        type=str,
        default="tecnologia",
        help="Chave da categoria (ex: tecnologia, notebooks, smartphones, hardware, perifericos, games, audio, smart_home, wearables). Padrão: tecnologia"
    )
    parser.add_argument(
        "--todas-categorias",
        action="store_true",
        help="Executa a busca em todas as categorias de tecnologia em sequência"
    )
    parser.add_argument(
        "-l", "--lojas",
        type=str,
        default="all",
        help="Lojas: all (todas), mercadolivre, amazon, shopee, aliexpress, kabum ou separadas por vírgula (ex: kabum,amazon). Padrão: all"
    )
    parser.add_argument(
        "--limite",
        type=int,
        default=30,
        help="Quantidade máxima de produtos por categoria (padrão: 30)"
    )
    parser.add_argument(
        "-f", "--formato",
        choices=["json", "csv", "all"],
        default="all",
        help="Formato de exportação: csv, json ou all (padrão: all)"
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Abre o navegador de forma visível durante a raspagem"
    )
    parser.add_argument(
        "--listar",
        action="store_true",
        help="Lista todas as categorias disponíveis e encerra"
    )

    args = parser.parse_args()

    if args.listar:
        exibir_tabela_categorias()
        return

    console.print("\n[bold green]============================================================[/bold green]")
    console.print("[bold yellow]🚀 Busca Especializada por Categorias - Marketplaces[/bold yellow]")
    console.print("[bold green]============================================================[/bold green]\n")

    categorias_alvo = []
    if args.todas_categorias:
        categorias_alvo = [cat["chave"] for cat in categories.listar_categorias()]
    else:
        categorias_alvo = [args.categoria]

    total_geral = []

    for cat_chave in categorias_alvo:
        produtos = categories.buscar_produtos_por_categoria(
            categoria_chave=cat_chave,
            plataformas=args.lojas,
            limite_por_termo=max(4, args.limite // 3),
            max_paginas=1,
            limite_total=args.limite,
            headless=not args.headful
        )

        if not produtos:
            console.print(f"[yellow]⚠️ Nenhum produto encontrado para a categoria '{cat_chave}'.[/yellow]")
            continue

        total_geral.extend(produtos)

        # Exibe resumo dos produtos encontrados
        display_summary_table(produtos, title=f"Categoria: {cat_chave.upper()} ({len(produtos)} itens)")

        # Exporta arquivos
        nome_arquivo = f"categoria_{cat_chave}"
        caminhos = categories.exportar_produtos(
            produtos=produtos,
            nome_base=nome_arquivo,
            formato=args.formato,
            pasta_destino="data"
        )
        for fmt, path in caminhos.items():
            console.print(f"  📄 Arquivo {fmt.upper()}: [cyan]{path}[/cyan]")

    if len(categorias_alvo) > 1 and total_geral:
        # Exporta também um consolidado geral de todas as categorias
        caminhos_geral = categories.exportar_produtos(
            produtos=total_geral,
            nome_base="todas_categorias_tecnologia",
            formato=args.formato,
            pasta_destino="data"
        )
        console.print("\n[bold green]📦 Exportação Consolidada de Todas as Categorias:[/bold green]")
        for fmt, path in caminhos_geral.items():
            console.print(f"  📄 Arquivo {fmt.upper()}: [bold cyan]{path}[/bold cyan]")

    console.print(f"\n[bold green]✨ Processo concluído! Total de {len(total_geral)} produtos coletados.[/bold green]\n")


if __name__ == "__main__":
    main()
