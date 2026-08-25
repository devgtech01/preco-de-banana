import json
import re
import sys
import pandas as pd

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

from rich.console import Console
from rich.table import Table

console = Console(force_terminal=True)

def clean_price(price_str: str) -> float | None:
    """
    Converte strings de preço do Mercado Livre (ex: "R$ 3.499,90", "3.499", "3499,90")
    para float (ex: 3499.90).
    """
    if not price_str:
        return None
    
    try:
        cleaned = re.sub(r'[^\d.,]', '', price_str).strip()
        if not cleaned:
            return None
            
        if ',' in cleaned:
            cleaned = cleaned.replace('.', '').replace(',', '.')
        elif cleaned.count('.') > 1:
            parts = cleaned.rsplit('.', 1)
            cleaned = parts[0].replace('.', '') + '.' + parts[1]
        elif cleaned.count('.') == 1:
            parts = cleaned.split('.')
            if len(parts[1]) == 3:
                cleaned = cleaned.replace('.', '')

        return float(cleaned)
    except Exception:
        return None

def calculate_stats(data: list[dict]) -> dict:
    """Calcula estatísticas de preço e frete para exibição nos cards da Web UI."""
    if not data:
        return {
            "total": 0,
            "menor_preco": 0.0,
            "maior_preco": 0.0,
            "preco_medio": 0.0,
            "pct_frete_gratis": 0
        }

    prices = [item["preco"] for item in data if isinstance(item.get("preco"), (int, float))]
    free_shipping_count = sum(1 for item in data if item.get("frete_gratis"))

    menor = min(prices) if prices else 0.0
    maior = max(prices) if prices else 0.0
    medio = sum(prices) / len(prices) if prices else 0.0
    pct_frete = round((free_shipping_count / len(data)) * 100) if data else 0

    return {
        "total": len(data),
        "menor_preco": round(menor, 2),
        "maior_preco": round(maior, 2),
        "preco_medio": round(medio, 2),
        "pct_frete_gratis": pct_frete
    }

def save_to_json(data: list[dict], filepath: str) -> None:
    """Salva a lista de dicionários em um arquivo JSON."""
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    console.print(f"[bold green]✓[/bold green] Dados salvos em JSON: [cyan]{filepath}[/cyan]")

def save_to_csv(data: list[dict], filepath: str) -> None:
    """Salva a lista de dicionários em um arquivo CSV usando Pandas."""
    if not data:
        console.print("[yellow]Aviso: Nenhum dado para salvar em CSV.[/yellow]")
        return
    df = pd.DataFrame(data)
    df.to_csv(filepath, index=False, encoding='utf-8-sig')
    console.print(f"[bold green]✓[/bold green] Dados salvos em CSV: [cyan]{filepath}[/cyan]")

def display_summary_table(data: list[dict], title: str = "Produtos Encontrados") -> None:
    """Exibe os resultados formatados em uma tabela no terminal com link para o produto."""
    if not data:
        console.print("[bold red]Nenhum produto para exibir.[/bold red]")
        return

    table = Table(title=f"🛒 {title} ({len(data)} itens)", show_lines=True, header_style="bold magenta")
    table.add_column("#", style="dim", width=3)
    table.add_column("Título", style="bold white", max_width=35)
    table.add_column("Preço (R$)", justify="right", style="green")
    table.add_column("Vendedor / Loja", style="cyan")
    table.add_column("Frete", justify="center")
    table.add_column("Link do Anúncio", style="blue underline", max_width=40)

    for idx, item in enumerate(data[:25], 1):
        price_display = f"R$ {item['preco']:.2f}" if isinstance(item.get('preco'), (int, float)) else "N/I"
        free_shipping = "[green]Grátis[/green]" if item.get('frete_gratis') else "[dim]Pago[/dim]"
        seller = item.get('vendedor') or "N/I"
        title_crop = item['titulo'][:32] + "..." if len(item.get('titulo', '')) > 35 else item.get('titulo', '')
        link = item.get('link') or ""
        link_crop = link[:38] + "..." if len(link) > 40 else link

        table.add_row(
            str(idx),
            title_crop,
            price_display,
            seller,
            free_shipping,
            link_crop
        )

    console.print(table)
    if len(data) > 25:
        console.print(f"[dim]* Exibindo os primeiros 25 resultados de {len(data)} no terminal.[/dim]")
