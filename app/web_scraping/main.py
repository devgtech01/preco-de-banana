import argparse
import os
import re
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

from rich.console import Console
import marketplaces
from utils import save_to_json, save_to_csv, display_summary_table

console = Console(force_terminal=True)

def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    return re.sub(r'[-\s]+', '_', text)

def main():
    parser = argparse.ArgumentParser(
        description="Multi-Marketplace Scraper (Mercado Livre, Amazon, Shopee, AliExpress e KaBuM!)."
    )
    parser.add_argument("-q", "--query", type=str, help="Termo de busca do produto (ex: 'notebook i7', 'rtx 4060')")
    parser.add_argument("-p", "--pages", type=int, default=1, help="Quantidade máxima de páginas (padrão: 1)")
    parser.add_argument("-l", "--limit", type=int, default=30, help="Quantidade máxima de itens (padrão: 30)")
    parser.add_argument(
        "--platform",
        default="both",
        help=(
            "Loja alvo: " + ", ".join(marketplaces.CHAVES) +
            ", 'both' (Mercado Livre + Amazon), 'all' (todas) ou várias "
            "separadas por vírgula (ex: shopee,kabum). Padrão: both"
        ),
    )
    parser.add_argument("-f", "--format", choices=["json", "csv", "all"], default="all", help="Formato de saída (padrão: all)")
    parser.add_argument("--headful", action="store_true", help="Executa com a janela do navegador visível")
    parser.add_argument("--web", action="store_true", help="Inicia a Interface Web (Bootstrap) em http://127.0.0.1:5000")

    args = parser.parse_args()

    if args.web or not args.query:
        console.print("[bold yellow]🌐 Iniciando Servidor Web Multi-Marketplace...[/bold yellow]")
        from app import app
        app.run(host='127.0.0.1', port=5000, debug=False)
        return

    query_slug = slugify(args.query)
    headless_mode = not args.headful
    chaves = marketplaces.resolver_plataformas(args.platform)
    cota = marketplaces.cota_por_loja(args.limit, len(chaves))

    console.print("[bold green]=================================================[/bold green]")
    console.print("[bold yellow]🛒 Multi-Marketplace Scraper[/bold yellow]")
    console.print("[bold green]=================================================[/bold green]")
    console.print(
        f"Termo: [cyan]{args.query}[/cyan] | Lojas: [cyan]{marketplaces.descricao(chaves)}[/cyan] "
        f"| Páginas: [cyan]{args.pages}[/cyan]\n"
    )

    por_loja = []
    for chave in chaves:
        scraper = marketplaces.criar_scraper(chave, headless=headless_mode)
        por_loja.append(scraper.search_products(query=args.query, max_pages=args.pages, max_items=cota))

    results = marketplaces.intercalar(por_loja)[:args.limit]

    if not results:
        console.print("[bold red]❌ Nenhum dado foi retornado da busca.[/bold red]")
        return

    display_summary_table(results, title=f"Resultados para '{args.query}' ({'+'.join(chaves).upper()})")

    os.makedirs("data", exist_ok=True)
    # O nome do arquivo usa as chaves resolvidas: 'shopee,kabum' viraria um
    # nome invalido no Windows por causa da virgula.
    sufixo = "_".join(chaves)
    json_path = os.path.join("data", f"produtos_{sufixo}_{query_slug}.json")
    csv_path = os.path.join("data", f"produtos_{sufixo}_{query_slug}.csv")

    if args.format in ["json", "all"]:
        save_to_json(results, json_path)

    if args.format in ["csv", "all"]:
        save_to_csv(results, csv_path)

    console.print("\n[bold green]✨ Raspagem concluída com sucesso![/bold green]")

if __name__ == "__main__":
    main()
