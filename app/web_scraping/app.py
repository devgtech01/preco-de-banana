import os
import random
import re
import secrets
import sys
import threading
import time
import requests
from functools import wraps
from flask import Flask, render_template, request, jsonify, send_from_directory, redirect, url_for, session
from dotenv import load_dotenv

# Carrega variáveis de ambiente do .env.local (se existir) ou .env
base_dir = os.path.dirname(os.path.abspath(__file__))
env_local_path = os.path.join(base_dir, ".env.local")
env_path = os.path.join(base_dir, ".env")

if os.path.exists(env_local_path):
    load_dotenv(env_local_path)
elif os.path.exists(env_path):
    load_dotenv(env_path)

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

import categories
import marketplaces
from utils import save_to_json, save_to_csv, calculate_stats

app = Flask(__name__)

# Sem SECRET_KEY definida, qualquer pessoa que conheça o valor padrão consegue
# forjar um cookie de sessão e entrar sem senha. Em produção isso é fatal, então
# geramos uma chave aleatória (invalida as sessões a cada restart, o que é o
# comportamento seguro) em vez de cair num literal público.
SECRET_KEY = os.getenv('SECRET_KEY')
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    print("⚠️  SECRET_KEY não definida no .env — usando chave aleatória temporária.", flush=True)
app.secret_key = SECRET_KEY

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    # Só marque como Secure quando houver HTTPS na frente, senão o navegador
    # descarta o cookie e o login entra em laço infinito.
    SESSION_COOKIE_SECURE=os.getenv('PORTAL_HTTPS', '').lower() in ('1', 'true', 'yes'),
)

DATA_DIR = os.path.join(base_dir, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# Credenciais de acesso configuradas via .env.local
PORTAL_USER = os.getenv('PORTAL_USERNAME', 'Admin')
PORTAL_PASS = os.getenv('PORTAL_PASSWORD')
if not PORTAL_PASS:
    PORTAL_PASS = secrets.token_urlsafe(16)
    print(f"⚠️  PORTAL_PASSWORD não definida. Senha temporária desta execução: {PORTAL_PASS}", flush=True)

def _flag(nome: str) -> bool:
    return (os.getenv(nome) or '').strip().lower() in ('1', 'true', 'yes', 'sim')


# Modo portátil: preso ao loopback, o login não protege nada — quem já está na
# máquina abre o .env.local do pendrive e lê a senha em dois cliques.
#
# A dispensa vale SOMENTE com o servidor escutando em 127.0.0.1. Se o portal for
# aberto para a rede (SOMENTE_ESTA_MAQUINA=0 no config.txt, ou o 0.0.0.0 do
# Docker), o login volta sozinho: sem isso, qualquer um do mesmo wifi publicaria
# no grupo de WhatsApp do dono.
_BIND = (os.getenv('PORTAL_HOST') or '0.0.0.0').strip()
SEM_LOGIN = _flag('SEM_LOGIN') and _BIND in ('127.0.0.1', 'localhost', '::1')

if _flag('SEM_LOGIN') and not SEM_LOGIN:
    print(f"⚠️  SEM_LOGIN ignorado: o portal escuta em {_BIND}, não só no loopback. "
          "Login continua obrigatório.", flush=True)
elif SEM_LOGIN:
    print("🔓 Sem tela de login (acesso restrito a esta máquina).", flush=True)

# Token compartilhado com o bot Node. Sem ele, qualquer pessoa que alcance a
# porta do bot consegue publicar no seu grupo e ler o token do Telegram.
BOT_API_TOKEN = os.getenv('BOT_API_TOKEN', '')

def bot_headers() -> dict:
    return {'X-Api-Token': BOT_API_TOKEN} if BOT_API_TOKEN else {}

# Um Firefox por vez. Cada scraping abre um navegador completo; requisições
# simultâneas multiplicavam isso até o container estourar o limite de memória.
SCRAPE_LOCK = threading.Lock()

def slugify(text: str) -> str:
    """Converte o termo de busca para um formato de slug válido para arquivos."""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    return re.sub(r'[-\s]+', '_', text)

# Cache da descoberta do bot. O frontend chama /api/bot-status a cada 3s e a
# versão anterior testava até 4 URLs com 1s de timeout em cada chamada — com o
# bot offline isso levava ~5s por requisição e empilhava chamadas no servidor.
_bot_url_cache = {'url': None, 'checked_at': 0.0}
_BOT_URL_TTL_OK = 300.0     # 5 min quando o bot respondeu
_BOT_URL_TTL_FAIL = 15.0    # 15s quando não respondeu

def get_active_bot_url(force: bool = False) -> str:
    """Descobre a URL do bot (Docker Compose bot-service:3005 ou local), com cache."""
    env_url = (os.getenv("BOT_API_URL") or "").rstrip('/')
    fallback = env_url or "http://bot-service:3005"

    now = time.time()
    ttl = _BOT_URL_TTL_OK if _bot_url_cache['url'] else _BOT_URL_TTL_FAIL
    if not force and (now - _bot_url_cache['checked_at']) < ttl:
        return _bot_url_cache['url'] or fallback

    candidates = [env_url] if env_url else []
    candidates += ["http://bot-service:3005", "http://127.0.0.1:3005"]

    found = None
    for url in candidates:
        try:
            resp = requests.get(f"{url}/api/health", timeout=2)
            if resp.status_code == 200:
                found = url
                break
        except Exception:
            continue

    _bot_url_cache['url'] = found
    _bot_url_cache['checked_at'] = now
    return found or fallback

def login_required(f):
    """Middleware decorator para exigir login no portal."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if SEM_LOGIN:
            return f(*args, **kwargs)
        if not session.get('isLoggedIn'):
            if request.path.startswith('/api/'):
                return jsonify({'success': False, 'error': 'Não autorizado. Efetue login no portal.'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# Freio simples de força bruta no login, por IP de origem.
_login_attempts: dict[str, list[float]] = {}
_LOGIN_WINDOW = 300.0
_LOGIN_MAX = 8

def _login_blocked(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _login_attempts.get(ip, []) if now - t < _LOGIN_WINDOW]
    _login_attempts[ip] = hits
    return len(hits) >= _LOGIN_MAX

def _register_failure(ip: str) -> None:
    _login_attempts.setdefault(ip, []).append(time.time())

@app.route('/login', methods=['GET', 'POST'])
def login():
    if SEM_LOGIN or session.get('isLoggedIn'):
        return redirect(url_for('index'))

    error = None
    if request.method == 'POST':
        ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'desconhecido').split(',')[0].strip()

        if _login_blocked(ip):
            return render_template('login.html', error='Muitas tentativas. Aguarde 5 minutos.'), 429

        user = request.form.get('username', '').strip()
        pwd = request.form.get('password', '').strip()

        # compare_digest evita vazar o tamanho/prefixo da senha pelo tempo de resposta.
        user_ok = secrets.compare_digest(user, PORTAL_USER)
        pass_ok = secrets.compare_digest(pwd, PORTAL_PASS)

        if user_ok and pass_ok:
            session.clear()
            session['isLoggedIn'] = True
            _login_attempts.pop(ip, None)
            return redirect(url_for('index'))
        else:
            _register_failure(ip)
            error = 'Usuário ou senha incorretos!'

    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    # Sem login não há de onde sair: mandar para /login criaria um pingue-pongue
    # (login redireciona de volta para o índice).
    if SEM_LOGIN:
        return redirect(url_for('index'))
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route('/api/health', methods=['GET'])
def health():
    """Endpoint sem autenticação usado pelo healthcheck do Docker."""
    return jsonify({'status': 'ok'})

@app.route('/api/bot-status', methods=['GET'])
@login_required
def get_bot_status():
    """Consulta a disponibilidade, métricas de VPS (CPU/RAM) e status do bot de divulgação (Node.js)."""
    bot_url = get_active_bot_url()
    try:
        resp = requests.get(f"{bot_url}/api/metrics", timeout=3, headers=bot_headers())
        if resp.status_code in [200, 302]:
            data = resp.json() if resp.status_code == 200 else {}
            return jsonify({
                'online': True,
                'waConnected': data.get('waConnected', False),
                'waQrUrl': data.get('waQrUrl'),
                'cpuPercent': data.get('cpuPercent', '0.0'),
                'memPercent': data.get('memPercent', '0.0'),
                'appMemMB': data.get('appMemMB', '0.0'),
                'message': 'Bot Conectado'
            })
    except Exception:
        pass
    return jsonify({
        'online': False,
        'waConnected': False,
        'waQrUrl': None,
        'cpuPercent': '0.0',
        'memPercent': '0.0',
        'appMemMB': '0.0',
        'message': 'Bot Offline'
    })

@app.route('/api/bot-settings', methods=['GET', 'POST'])
@login_required
def handle_bot_settings():
    """
    Proxy para ler ou atualizar configurações de afiliados no bot.

    O GET pede `mask=1`: o token do Telegram e o App Secret da Shopee chegam
    aqui como "••••••••" e é isso que vai para o navegador. Quem precisa do
    segredo de verdade é o shopee_api.py, que fala com o bot direto — não
    passa por este proxy.
    """
    bot_url = get_active_bot_url()
    try:
        if request.method == 'POST':
            resp = requests.post(f"{bot_url}/api/bot-settings", json=request.get_json(), timeout=8, headers=bot_headers())
        else:
            resp = requests.get(f"{bot_url}/api/bot-settings", params={'mask': '1'}, timeout=8, headers=bot_headers())
        return jsonify(resp.json()), resp.status_code
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/affiliate-schema')
@login_required
def affiliate_schema():
    """Lojas e campos que o bot conhece, para o painel se desenhar sozinho."""
    bot_url = get_active_bot_url()
    try:
        resp = requests.get(f"{bot_url}/api/affiliate-schema", timeout=8, headers=bot_headers())
        return jsonify(resp.json()), resp.status_code
    except Exception as e:
        return jsonify({'success': False, 'lojas': [], 'error': str(e)}), 500


@app.route('/api/wa-groups')
@login_required
def wa_groups():
    """Grupos do WhatsApp, para a tela oferecer uma lista em vez do JID cru."""
    bot_url = get_active_bot_url()
    try:
        resp = requests.get(f"{bot_url}/api/wa-groups", timeout=15, headers=bot_headers())
        return jsonify(resp.json()), resp.status_code
    except Exception as e:
        return jsonify({'success': False, 'grupos': [], 'error': str(e)}), 500

@app.route('/api/resolve-link', methods=['POST'])
@login_required
def resolve_link():
    """
    Link colado a mao -> oferta pronta, com o endereco de afiliado aplicado.

    A busca continua sendo o caminho normal, mas oferta que chega por fora
    (indicacao de outro grupo, link do proprio celular) nao tinha por onde
    passar pelo afiliado antes de ir para o grupo. Quem raspa a pagina e aplica
    a regra da loja e o bot, que e o dono do settings.db: aqui so repassamos.
    """
    bot_url = get_active_bot_url()
    try:
        resp = requests.post(
            f"{bot_url}/api/resolve-link",
            json=request.get_json() or {},
            timeout=30,
            headers=bot_headers()
        )
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({'success': False, 'error': f'Servidor do Bot ({bot_url}) nao respondeu.'}), 503
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/publish', methods=['POST'])
@login_required
def publish_deal():
    """Envia um único produto para publicação via bot-divulgar-produtos."""
    bot_url = get_active_bot_url()
    try:
        product = request.get_json() or {}
        if not product.get('titulo') or not product.get('link'):
            return jsonify({'success': False, 'error': 'Produto inválido para publicação.'}), 400

        resp = requests.post(f"{bot_url}/api/post-deal", json=product, timeout=30, headers=bot_headers())
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({'success': False, 'error': f'Servidor do Bot ({bot_url}) não respondeu.'}), 503
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/publish-batch', methods=['POST'])
@login_required
def publish_batch():
    """Envia múltiplos produtos em lote para publicação via bot-divulgar-produtos."""
    bot_url = get_active_bot_url()
    try:
        body = request.get_json() or {}
        products = body.get('products', [])

        if not products:
            return jsonify({'success': False, 'error': 'Nenhum produto selecionado.'}), 400

        published_count = 0
        errors = []

        # Intervalo entre disparos. 1 mensagem por segundo é o caminho mais curto
        # para o WhatsApp banir o número; o padrão passa a ser 6-14s com variação.
        min_gap = float(os.getenv('PUBLISH_MIN_DELAY', '6'))
        max_gap = float(os.getenv('PUBLISH_MAX_DELAY', '14'))

        for index, prod in enumerate(products):
            try:
                resp = requests.post(f"{bot_url}/api/post-deal", json=prod, timeout=30, headers=bot_headers())
                res_data = resp.json()
                if res_data.get('success'):
                    published_count += 1
                else:
                    errors.append(f"{prod.get('titulo', 'Item')[:30]}: {res_data.get('message', 'Erro')}")
            except Exception as err:
                errors.append(f"{prod.get('titulo', 'Item')[:30]}: {str(err)}")

            if index < len(products) - 1:
                time.sleep(random.uniform(min_gap, max_gap))

        return jsonify({
            'success': published_count > 0,
            'published_count': published_count,
            'total': len(products),
            'errors': errors,
            'message': f"Publicadas {published_count} de {len(products)} ofertas selecionadas."
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/scrape', methods=['POST'])
@login_required
def run_scrape():
    try:
        body = request.get_json() or {}
        query = body.get('query', 'notebook i7').strip()
        pages = int(body.get('pages', 1))
        limit = int(body.get('limit', 30))
        platform = body.get('platform', 'both').lower()
        headful = bool(body.get('headful', False))

        if not query:
            return jsonify({'success': False, 'error': 'O termo de busca não pode estar vazio.'}), 400

        # Só uma busca por vez: duas em paralelo abriam dois Firefox e
        # derrubavam o container por falta de memória.
        if not SCRAPE_LOCK.acquire(blocking=False):
            return jsonify({
                'success': False,
                'error': 'Já existe uma busca em andamento. Aguarde ela terminar.'
            }), 429

        try:
            warnings = []

            # A lista de lojas vem do registro em marketplaces.py: incluir uma
            # loja nova nao exige mexer aqui. 'both' continua sendo Mercado
            # Livre + Amazon e 'all' cobre todas as cinco.
            chaves = marketplaces.resolver_plataformas(platform)
            cota = marketplaces.cota_por_loja(limit, len(chaves))

            por_loja = []
            for chave in chaves:
                scraper = marketplaces.criar_scraper(chave, headless=not headful)
                por_loja.append(
                    scraper.search_products(query=query, max_pages=pages, max_items=cota)
                )
                if scraper.last_error:
                    warnings.append(scraper.last_error)

            # Intercalado: as primeiras linhas da tabela ja trazem uma amostra
            # de cada loja, em vez de todo o Mercado Livre e so depois o resto.
            results = marketplaces.intercalar(por_loja)[:limit]
        finally:
            SCRAPE_LOCK.release()

        stats = calculate_stats(results)

        # Retorna apenas em memória, sem salvar nenhum arquivo no disco da VPS
        return jsonify({
            'success': True,
            'data': results,
            'stats': stats,
            'query': query,
            'platform': platform,
            # Motivo explícito quando a busca volta vazia — antes o portal só
            # mostrava "0 produtos" sem dizer se foi bloqueio, timeout ou seletor.
            'warnings': warnings,
            'error': ' | '.join(warnings) if warnings and not results else None
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/categories', methods=['GET'])
@login_required
def get_categories():
    """Retorna as categorias disponíveis para busca especializada (com foco em Tecnologia)."""
    try:
        cats = categories.listar_categorias()
        return jsonify({'success': True, 'categories': cats})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/scrape-category', methods=['POST'])
@login_required
def run_scrape_category():
    """Executa a busca especializada dos principais produtos de uma categoria em múltiplos marketplaces."""
    try:
        body = request.get_json() or {}
        category_key = body.get('category', 'tecnologia').strip()
        platform = body.get('platform', 'all')
        limit = int(body.get('limit', 30))
        headful = bool(body.get('headful', False))

        if not SCRAPE_LOCK.acquire(blocking=False):
            return jsonify({
                'success': False,
                'error': 'Já existe uma busca em andamento. Aguarde ela terminar.'
            }), 429

        try:
            results = categories.buscar_produtos_por_categoria(
                categoria_chave=category_key,
                plataformas=platform,
                limite_por_termo=max(4, limit // 3),
                max_paginas=1,
                limite_total=limit,
                headless=not headful
            )
        finally:
            SCRAPE_LOCK.release()

        stats = calculate_stats(results)

        cat_info = categories.CATEGORIAS.get(category_key, {})
        cat_nome = cat_info.get("nome", category_key.capitalize())
        cat_emoji = cat_info.get("emoji", "💻")

        return jsonify({
            'success': True,
            'data': results,
            'stats': stats,
            'category': category_key,
            'category_name': f"{cat_emoji} {cat_nome}",
            'platform': platform,
            'error': 'Nenhum produto encontrado para a categoria selecionada.' if not results else None
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/export/csv', methods=['POST'])
@login_required
def export_csv():
    """Gera o arquivo CSV formatado com nome do produto, link, valor de antes e depois."""
    try:
        import io
        import pandas as pd
        from flask import Response

        body = request.get_json() or {}
        products = body.get('products', [])
        category_name = body.get('category', 'produtos')

        if not products:
            return jsonify({'success': False, 'error': 'Nenhum produto para exportar.'}), 400

        # Padroniza colunas em ordem de prioridade
        colunas_ordem = [
            "nome_produto",
            "link",
            "valor_antes",
            "valor_depois",
            "desconto",
            "categoria",
            "plataforma",
            "frete_gratis",
            "cupom",
            "vendedor",
            "condicao"
        ]

        linhas = []
        for p in products:
            nome = p.get("nome_produto") or p.get("titulo") or ""
            link = p.get("link") or ""
            valor_antes = p.get("valor_antes") if p.get("valor_antes") is not None else p.get("preco_original")
            valor_depois = p.get("valor_depois") if p.get("valor_depois") is not None else p.get("preco")
            desconto = p.get("desconto") or p.get("destaque")
            if not desconto and valor_antes and valor_depois and valor_antes > valor_depois:
                pct = round(((valor_antes - valor_depois) / valor_antes) * 100)
                desconto = f"{pct}% OFF"

            item = {
                "nome_produto": nome,
                "link": link,
                "valor_antes": valor_antes,
                "valor_depois": valor_depois,
                "desconto": desconto,
                "categoria": p.get("categoria", "Tecnologia"),
                "plataforma": p.get("plataforma", ""),
                "frete_gratis": "Sim" if p.get("frete_gratis") else "Não",
                "cupom": p.get("cupom") or "",
                "vendedor": p.get("vendedor") or "",
                "condicao": p.get("condicao") or "Novo"
            }
            linhas.append(item)

        df = pd.DataFrame(linhas)
        output = io.StringIO()
        df.to_csv(output, index=False, encoding='utf-8-sig')

        filename = f"{slugify(category_name)}_export.csv"

        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment;filename={filename}"}
        )
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/export/json', methods=['POST'])
@login_required
def export_json():
    """Gera o arquivo JSON formatado com nome do produto, link, valor de antes e depois."""
    try:
        import json
        from flask import Response

        body = request.get_json() or {}
        products = body.get('products', [])
        category_name = body.get('category', 'produtos')

        if not products:
            return jsonify({'success': False, 'error': 'Nenhum produto para exportar.'}), 400

        linhas = []
        for p in products:
            nome = p.get("nome_produto") or p.get("titulo") or ""
            link = p.get("link") or ""
            valor_antes = p.get("valor_antes") if p.get("valor_antes") is not None else p.get("preco_original")
            valor_depois = p.get("valor_depois") if p.get("valor_depois") is not None else p.get("preco")
            desconto = p.get("desconto") or p.get("destaque")
            if not desconto and valor_antes and valor_depois and valor_antes > valor_depois:
                pct = round(((valor_antes - valor_depois) / valor_antes) * 100)
                desconto = f"{pct}% OFF"

            item = {
                "nome_produto": nome,
                "link": link,
                "valor_antes": valor_antes,
                "valor_depois": valor_depois,
                "desconto": desconto,
                "categoria": p.get("categoria", "Tecnologia"),
                "plataforma": p.get("plataforma", ""),
                "frete_gratis": bool(p.get("frete_gratis")),
                "cupom": p.get("cupom") or None,
                "vendedor": p.get("vendedor") or "",
                "condicao": p.get("condicao") or "Novo",
                "imagem_url": p.get("imagem_url") or None
            }
            linhas.append(item)

        json_str = json.dumps(linhas, ensure_ascii=False, indent=2)
        filename = f"{slugify(category_name)}_export.json"

        return Response(
            json_str,
            mimetype="application/json",
            headers={"Content-Disposition": f"attachment;filename={filename}"}
        )
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == '__main__':
    # Execução direta serve apenas para desenvolvimento local.
    # Em container o entrypoint é o gunicorn (ver Dockerfile); no modo portátil
    # (Windows, pendrive) é o servir.py, porque gunicorn não roda em Windows.
    host = os.getenv('PORTAL_HOST', '0.0.0.0')
    port = int(os.getenv('PORTAL_PORT', '5000'))
    print(f"\n🚀 Portal Único Protegido rodando em: http://127.0.0.1:{port}\n")
    app.run(host=host, port=port, debug=False, threaded=True)
