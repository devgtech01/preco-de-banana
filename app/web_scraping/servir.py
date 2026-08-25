"""
Servidor de produção para Windows (modo portátil / pendrive).

O Dockerfile sobe o portal com gunicorn, que depende de `fcntl` e por isso não
existe em Windows. Aqui o equivalente é o waitress.

Regras que precisam continuar valendo fora do container:
  - um processo só: o lock que impede dois Firefox simultâneos (SCRAPE_LOCK em
    app.py) vale por processo. Com dois workers ele deixaria de valer e a
    máquina abriria dois navegadores na mesma busca.
  - timeout largo: uma raspagem com retentativa e backoff passa fácil de 30s.
"""

import os
import sys

# O Python "embeddable" (modo portátil) monta o sys.path só a partir do arquivo
# ._pth: diferente do Python normal, ele não inclui a pasta do próprio script.
# Sem esta linha o `from app import app` abaixo falha com ModuleNotFoundError.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from app import app  # noqa: E402  (carrega .env.local antes de qualquer coisa)

HOST = os.getenv('PORTAL_HOST', '127.0.0.1')
PORT = int(os.getenv('PORTAL_PORT', '5000'))

if __name__ == '__main__':
    try:
        from waitress import serve
    except ImportError:
        print("⚠️  waitress não instalado — caindo no servidor embutido do Flask.", flush=True)
        print(f"🚀 Portal em http://127.0.0.1:{PORT}\n", flush=True)
        app.run(host=HOST, port=PORT, debug=False, threaded=True)
    else:
        print(f"🚀 Portal em http://127.0.0.1:{PORT}\n", flush=True)
        serve(
            app,
            host=HOST,
            port=PORT,
            threads=8,             # atende o polling de status durante uma busca
            channel_timeout=600,   # equivalente ao --timeout 600 do gunicorn
            ident='portal',
        )
