# --- Estágio de build: compila as dependências nativas (sqlite3) ---
FROM node:20-slim AS builder

WORKDIR /app

# git é exigido por dependências instaladas via repositório;
# python3/make/g++ cobrem o caso de o sqlite3 não achar binário pré-compilado.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git python3 make g++ ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY package*.json ./

# npm ci quando existe package-lock.json (build reprodutível), senão npm install.
RUN if [ -f package-lock.json ]; then npm ci --omit=dev; else npm install --omit=dev; fi

# --- Estágio final: só o runtime, sem compiladores ---
FROM node:20-slim

ENV NODE_ENV=production \
    TZ=America/Sao_Paulo \
    PORT=3005

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/node_modules ./node_modules
COPY . .

# Diretórios com volume: banco de configurações e sessão do WhatsApp.
RUN mkdir -p /app/data /app/auth_info_baileys

EXPOSE 3005

# tini como PID 1 para que SIGTERM chegue ao Node e o container pare limpo.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["node", "server.js"]
