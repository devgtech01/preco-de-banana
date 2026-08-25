# A versão da imagem PRECISA bater com a do pacote playwright do requirements.
# Antes a base era v1.44 e o pip instalava >=1.50: os binários de navegador
# empacotados na imagem eram de outra geração da biblioteca, e o
# `playwright install firefox` rodava sem `--with-deps`, deixando o container
# sujeito a falhar no launch por biblioteca de sistema ausente.
FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/Sao_Paulo

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Garante o Firefox e TODAS as dependências de sistema dele.
RUN playwright install --with-deps firefox

COPY . .

RUN mkdir -p /app/data/profiles

EXPOSE 5000

# gunicorn no lugar do servidor de desenvolvimento do Flask.
#   --workers 1  : o lock que impede dois Firefox simultâneos é por processo;
#                  com mais de um worker ele deixaria de valer e o container
#                  estouraria o limite de memória.
#   --threads 4  : atende o polling de status enquanto uma busca está rodando.
#   --timeout 600: uma raspagem com retentativa e backoff passa fácil de 30s.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", \
     "--timeout", "600", "--graceful-timeout", "30", "--access-logfile", "-", "app:app"]
