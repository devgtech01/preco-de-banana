require('dotenv').config();
const express = require('express');
const axios = require('axios');
const path = require('path');
const { getSettings, updateSettings } = require('./database');
const { construirLink, rotuloDaPlataforma, linkDaLoja } = require('./afiliados');
const { scrapeProduct } = require('./scraper');
const { pino } = require('pino');
const { default: makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion, Browsers } = require('@whiskeysockets/baileys');
const qrcode = require('qrcode-terminal');
const QRCodeWeb = require('qrcode');
const fs = require('fs');
const crypto = require('crypto');
const session = require('express-session');
const FormData = require('form-data');

const app = express();
// O docker-compose publica 3005 e o Dockerfile expõe 3005: o padrão precisa
// bater, senão sem um .env presente o bot sobe em 3000 e fica inalcançável.
const PORT = process.env.PORT || 3005;

// Interface de escuta. O padrão 0.0.0.0 é o que o Docker precisa (a porta é
// publicada pelo compose só no loopback da VPS). No modo portátil o launcher
// define BIND_HOST=127.0.0.1: rodando em rede alheia — casa de terceiro, wifi
// de trabalho — ninguém do mesmo wifi alcança o painel nem /api/post-deal.
const BIND_HOST = process.env.BIND_HOST || '0.0.0.0';

const flagLigada = (v) => ['1', 'true', 'yes', 'sim'].includes(String(v || '').trim().toLowerCase());

// Mesmo raciocínio do portal: preso ao loopback, a tela de login não protege
// nada — quem já está na máquina lê a senha no .env em dois cliques. Só vale
// escutando em 127.0.0.1; aberto para a rede, o login volta sozinho.
const SEM_LOGIN = flagLigada(process.env.SEM_LOGIN)
    && ['127.0.0.1', 'localhost', '::1'].includes(BIND_HOST);

if (flagLigada(process.env.SEM_LOGIN) && !SEM_LOGIN) {
    console.warn(`⚠️  SEM_LOGIN ignorado: o bot escuta em ${BIND_HOST}, não só no loopback.`);
}

// Token compartilhado com o portal Python. Sem ele as rotas de máquina ficam
// abertas: qualquer um que alcance a porta publica no seu grupo de WhatsApp e
// lê o token do Telegram em texto puro.
const API_TOKEN = process.env.BOT_API_TOKEN || '';
if (!API_TOKEN) {
    console.warn('⚠️  BOT_API_TOKEN não definido — as rotas /api/* ficarão SEM autenticação.');
    console.warn('   Defina BOT_API_TOKEN no .env do bot e no do portal antes de expor a porta.');
}

// CORS restrito. "*" combinado com rotas sem auth permitia que qualquer site
// aberto no navegador disparasse publicações.
const ALLOWED_ORIGINS = (process.env.ALLOWED_ORIGINS || '')
    .split(',')
    .map(o => o.trim())
    .filter(Boolean);

app.use((req, res, next) => {
    const origin = req.headers.origin;
    if (origin && ALLOWED_ORIGINS.includes(origin)) {
        res.header("Access-Control-Allow-Origin", origin);
        res.header("Vary", "Origin");
    }
    res.header("Access-Control-Allow-Headers", "Origin, X-Requested-With, Content-Type, Accept, X-Api-Token");
    res.header("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    if (req.method === 'OPTIONS') return res.sendStatus(200);
    next();
});

// Exige o token nas rotas consumidas pelo portal (server-to-server).
const requireApiToken = (req, res, next) => {
    if (!API_TOKEN) return next();

    const provided = req.get('X-Api-Token') || '';
    const expected = API_TOKEN;
    const a = Buffer.from(provided);
    const b = Buffer.from(expected);

    if (a.length === b.length && crypto.timingSafeEqual(a, b)) return next();

    // Sessão de navegador logada também vale, para as telas do próprio bot.
    if (req.session && req.session.isLoggedIn) return next();

    return res.status(401).json({ success: false, error: 'Token de API inválido ou ausente.' });
};

// Inicialização do WhatsApp Client com Baileys
let sock;
let isWhatsAppConnected = false;
let globalQRUrl = null;

const AUTH_DIR = path.join(__dirname, 'auth_info_baileys');

/**
 * Limpa as credenciais do WhatsApp sem remover a própria pasta.
 *
 * No Docker esse diretório é um bind mount: apagá-lo com rmSync(dir) devolve
 * EBUSY, a exceção estoura dentro do handler do Baileys e derruba o processo,
 * deixando o container em laço de restart.
 */
function clearAuthState() {
    try {
        if (!fs.existsSync(AUTH_DIR)) {
            fs.mkdirSync(AUTH_DIR, { recursive: true });
            return;
        }
        for (const entry of fs.readdirSync(AUTH_DIR)) {
            fs.rmSync(path.join(AUTH_DIR, entry), { recursive: true, force: true });
        }
    } catch (err) {
        console.error('Falha ao limpar as credenciais do WhatsApp:', err.message);
    }
}

async function connectToWhatsApp() {
    fs.mkdirSync(AUTH_DIR, { recursive: true });
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
    const { version, isLatest } = await fetchLatestBaileysVersion();
    
    sock = makeWASocket({
        version,
        auth: state,
        browser: Browsers.macOS('Desktop'),
        printQRInTerminal: false,
        logger: require('pino')({ level: 'silent' }) // Voltando ao silencioso
    });
    
    sock.ev.on('creds.update', saveCreds);
    
    sock.ev.on('connection.update', (update) => {
        const { connection, lastDisconnect, qr } = update;
        
        if (qr) {
            console.log('\n--- SCAN THE QR CODE BELOW TO CONNECT WHATSAPP ---');
            qrcode.generate(qr, { small: true });
            QRCodeWeb.toDataURL(qr, (err, url) => {
                if (!err) globalQRUrl = url;
            });
        }
        
        if (connection === 'close') {
            const shouldReconnect = lastDisconnect?.error?.output?.statusCode !== DisconnectReason.loggedOut;
            console.log('--- WHATSAPP CONNECTION CLOSED! Reconnecting:', shouldReconnect, '---');
            isWhatsAppConnected = false;
            globalQRUrl = null;
            if (shouldReconnect) {
                scheduleReconnect(3000); // Tentar reconectar em 3 seg
            } else {
                // Foi deslogado do celular. Apagar credenciais antigas e reconectar.
                clearAuthState();
                console.log('--- SESSION DELETED. RESTARTING ---');
                scheduleReconnect(3000);
            }
        } else if (connection === 'open') {
            console.log('--- WHATSAPP BOT IS READY! ---');
            isWhatsAppConnected = true;
            globalQRUrl = null;
        }
    });
}

/**
 * Reagenda a conexão tratando a rejeição da promise.
 *
 * connectToWhatsApp() era chamado "solto": qualquer falha (rede caída na
 * subida do container, por exemplo) virava unhandled rejection e o Node 20
 * encerra o processo nesse caso.
 */
function scheduleReconnect(delay) {
    setTimeout(() => {
        connectToWhatsApp().catch(err => {
            console.error('Falha ao conectar no WhatsApp:', err.message);
            scheduleReconnect(Math.min(delay * 2, 60000));
        });
    }, delay);
}

scheduleReconnect(0);

// Rede instável ou API do WhatsApp fora do ar não podem derrubar o servidor
// HTTP inteiro — sem isso o portal passa a mostrar "Bot Offline".
process.on('unhandledRejection', (reason) => {
    console.error('Unhandled rejection:', reason && reason.message ? reason.message : reason);
});
process.on('uncaughtException', (err) => {
    console.error('Uncaught exception:', err && err.message ? err.message : err);
});

app.set('view engine', 'ejs');
app.set('views', path.join(__dirname, 'views'));
app.use(express.urlencoded({ limit: '50mb', extended: true }));
app.use(express.json({ limit: '50mb' }));
app.use(express.static(path.join(__dirname, 'public')));

// Configurações de Sessão.
// Sem SESSION_SECRET o valor literal antigo era público: bastava conhecê-lo
// para assinar um cookie válido e entrar sem senha. Agora cai numa chave
// aleatória (as sessões morrem no restart, que é o comportamento seguro).
const SESSION_SECRET = process.env.SESSION_SECRET || crypto.randomBytes(32).toString('hex');
if (!process.env.SESSION_SECRET) {
    console.warn('⚠️  SESSION_SECRET não definido — usando chave aleatória temporária.');
}

app.use(session({
    secret: SESSION_SECRET,
    resave: false,
    saveUninitialized: false,
    cookie: {
        httpOnly: true,
        sameSite: 'lax',
        secure: String(process.env.BOT_HTTPS || '').toLowerCase() === 'true',
        maxAge: 24 * 60 * 60 * 1000 // 24 horas
    }
}));

// Middleware de Autenticação
const requireAuth = (req, res, next) => {
    if (SEM_LOGIN || req.session.isLoggedIn) {
        next();
    } else {
        res.redirect('/login');
    }
};

// Rotas de Autenticação
app.get('/login', (req, res) => {
    if (SEM_LOGIN || req.session.isLoggedIn) return res.redirect('/');
    res.render('login', { error: null });
});

const ADMIN_USER = process.env.ADMIN_USERNAME || 'admin';
const ADMIN_PASS = process.env.ADMIN_PASSWORD || crypto.randomBytes(12).toString('base64url');
if (!process.env.ADMIN_PASSWORD) {
    console.warn(`⚠️  ADMIN_PASSWORD não definida. Senha temporária desta execução: ${ADMIN_PASS}`);
}

// Freio de força bruta por IP.
const loginAttempts = new Map();
const LOGIN_WINDOW_MS = 5 * 60 * 1000;
const LOGIN_MAX = 8;

function loginBlocked(ip) {
    const now = Date.now();
    const hits = (loginAttempts.get(ip) || []).filter(t => now - t < LOGIN_WINDOW_MS);
    loginAttempts.set(ip, hits);
    return hits.length >= LOGIN_MAX;
}

function safeEqual(a, b) {
    const bufA = Buffer.from(String(a));
    const bufB = Buffer.from(String(b));
    return bufA.length === bufB.length && crypto.timingSafeEqual(bufA, bufB);
}

app.post('/login', (req, res) => {
    const { username, password } = req.body;
    const ip = (req.headers['x-forwarded-for'] || req.socket.remoteAddress || 'desconhecido').split(',')[0].trim();

    if (loginBlocked(ip)) {
        return res.status(429).render('login', { error: 'Muitas tentativas. Aguarde 5 minutos.' });
    }

    if (safeEqual(username || '', ADMIN_USER) && safeEqual(password || '', ADMIN_PASS)) {
        req.session.isLoggedIn = true;
        loginAttempts.delete(ip);
        res.redirect('/');
    } else {
        loginAttempts.set(ip, [...(loginAttempts.get(ip) || []), Date.now()]);
        res.render('login', { error: 'Usuário ou senha incorretos!' });
    }
});

app.get('/logout', (req, res) => {
    req.session.destroy();
    // Sem login não há de onde sair: /login devolveria para cá em seguida.
    res.redirect(SEM_LOGIN ? '/' : '/login');
});

app.get('/', requireAuth, (req, res) => {
    res.render('index', { success: null, error: null });
});

// Healthcheck: sem autenticação e sem custo, usado pelo Docker e pela
// descoberta de serviço do portal.
app.get('/api/health', (req, res) => {
    res.json({ status: 'ok', waConnected: isWhatsAppConnected });
});

/**
 * Credenciais efetivas de disparo.
 * O banco (com volume) tem prioridade; as variáveis de ambiente ficam como
 * valor inicial para quem já tinha tudo configurado no .env.
 */
async function resolveCredentials() {
    const settings = await getSettings();
    return {
        botToken: settings.telegramBotToken || process.env.TELEGRAM_BOT_TOKEN || '',
        telegramChatId: settings.telegramChatId || process.env.TELEGRAM_CHAT_ID || '',
        whatsappGroupId: settings.whatsappGroupId || process.env.WHATSAPP_CHAT_ID || '',
        settings
    };
}

function telegramReady(botToken, chatId) {
    return !!botToken && !!chatId && botToken !== 'SEU_TOKEN_DO_BOT_AQUI';
}

app.get('/api/metrics', requireApiToken, (req, res) => {
    const osUtils = require('os-utils');
    osUtils.cpuUsage(function(v) {
        res.json({
            cpuPercent: (v * 100).toFixed(1),
            memPercent: (100 - (osUtils.freememPercentage() * 100)).toFixed(1),
            appMemMB: (process.memoryUsage().rss / 1024 / 1024).toFixed(1),
            waConnected: isWhatsAppConnected,
            waQrUrl: globalQRUrl
        });
    });
});

app.get('/api/bot-settings', requireApiToken, async (req, res) => {
    try {
        const { botToken, telegramChatId, whatsappGroupId, settings } = await resolveCredentials();
        res.json({
            success: true,
            settings: {
                ...settings,
                telegramBotToken: botToken,
                telegramChatId,
                whatsappGroupId
            }
        });
    } catch (err) {
        res.status(500).json({ success: false, error: err.message });
    }
});

app.post('/api/bot-settings', requireApiToken, async (req, res) => {
    try {
        // Tudo vai para o SQLite, que está em ./data com volume.
        // A versão anterior gravava as credenciais num .env DENTRO do container:
        // funcionava até o primeiro `docker compose up --build`, e então sumia.
        //
        // O corpo inteiro é repassado porque updateSettings só grava as colunas
        // conhecidas (UPDATABLE) — assim um campo de afiliado novo não precisa
        // ser listado também aqui, que era onde o esquecimento acontecia.
        await updateSettings(req.body || {});

        const updated = await resolveCredentials();
        res.json({
            success: true,
            settings: {
                ...updated.settings,
                telegramBotToken: updated.botToken,
                telegramChatId: updated.telegramChatId,
                whatsappGroupId: updated.whatsappGroupId
            },
            message: 'Configurações e credenciais salvas com sucesso!'
        });
    } catch (err) {
        res.status(500).json({ success: false, error: err.message });
    }
});

app.get('/settings', requireAuth, async (req, res) => {
    try {
        const settings = await getSettings();
        res.render('settings', { settings, success: null, error: null });
    } catch (err) {
        res.render('settings', { settings: {}, success: null, error: 'Erro ao conectar banco de dados.' });
    }
});

app.post('/settings', requireAuth, async (req, res) => {
    try {
        await updateSettings(req.body);
        const settings = await getSettings();
        res.render('settings', { settings, success: 'Configurações salvas com sucesso!', error: null });
    } catch (err) {
        const settings = await getSettings();
        res.render('settings', { settings, success: null, error: 'Erro ao salvar configurações.' });
    }
});

app.post('/api/generate-preview', requireAuth, async (req, res) => {
    const { url, coupon, mode, storeName, imageBase64 } = req.body;
    
    if (mode === 'coupon') {
        if (!storeName || !coupon || !imageBase64) {
             return res.render('index', { success: null, error: 'Preencha a loja e o cupom para gerar a arte.' });
        }
        
        try {
            const credentials = await resolveCredentials();
            const settings = credentials.settings;

            // A arte de cupom divulga a loja, não um produto: o link é a
            // entrada da loja já com a afiliação aplicada (ver afiliados.js).
            const affiliateLink = linkDaLoja(storeName, settings);
            
            const currentHour = new Date().getHours();
            let periodText = 'CUPOM EXCLUSIVO';
            if (currentHour >= 0 && currentHour < 6) periodText = 'CUPOM DA MADRUGADA';
            else if (currentHour >= 6 && currentHour < 12) periodText = 'CUPOM DA MANHÃ';
            else if (currentHour >= 12 && currentHour < 18) periodText = 'CUPOM DA TARDE';
            else if (currentHour >= 18 && currentHour <= 23) periodText = 'CUPOM DA NOITE';

            const caption = `🔥 <b>${periodText} LIBERADO NA ${storeName.toUpperCase()}!</b>\n\n🎟️ <b>Use o Cupom:</b> <code>${coupon.trim()}</code>\n\n🛒 <b>Aproveite aqui:</b> <a href="${affiliateLink}">Link Ativar Cupom</a>`;

            return res.render('preview', { 
                product: {
                    image: imageBase64, // Pega a string Base64 e injeta no preview
                    caption: caption,
                    title: `Cupom ${storeName}`
                },
                config: {
                    telegram: telegramReady(credentials.botToken, credentials.telegramChatId),
                    whatsapp: (isWhatsAppConnected && !!credentials.whatsappGroupId)
                },
                success: null,
                error: null
            });
        } catch (err) {
            console.error('Erro na extração de cupom:', err.message);
            return res.render('index', { success: null, error: `Erro: ${err.message}` });
        }
    }

    // MODO NORMAL DE PRODUTO:
    if (!url) {
         return res.render('index', { success: null, error: 'O Link não pode estar vazio.' });
    }

    try {
        const credentials = await resolveCredentials();
        const productData = await scrapeProduct(url, credentials.settings);

        // Tratar o título para não quebrar o HTML do Telegram (remove caracteres perigosos)
        const safeTitle = productData.title.replace(/[<>]/g, '');

        // Montamos a parte do preço ("De/Por" se existir desconto)
        let priceText = `💸 <b>Por apenas: ${productData.price}</b>`;
        if (productData.originalPrice && productData.originalPrice !== productData.price) {
            priceText = `📉 BAIXOU! Oportunidade Única:\n❌ De: <s>${productData.originalPrice}</s>\n✅ <b>Por: ${productData.price}</b>`;
        }

        // Insere o cupom se o usuário preencheu no formulário
        let couponText = '';
        if (coupon && coupon.trim().length > 0) {
            // A tag <code> do HTML do Telegram faz a fonte ficar mono e aparecer a dica de "toque para copiar" no celular
            couponText = `\n🎟️ <b>Use o Cupom:</b> <code>${coupon.trim()}</code>\n`;
        }

        // Montamos o template principal
        const caption = `🔥 <b>Oferta Imperdível!</b>\n\n📦 ${safeTitle}\n\n${priceText}\n${couponText}\n🛒 <b>Compre aqui:</b> <a href="${productData.affiliateUrl}">Link com Desconto</a>`;

        // Passamos os dados para a tela de prévia ao invés de enviar logo
        res.render('preview', { 
            product: {
                image: productData.image,
                caption: caption,
                title: safeTitle
            },
            config: {
                telegram: telegramReady(credentials.botToken, credentials.telegramChatId),
                whatsapp: (isWhatsAppConnected && !!credentials.whatsappGroupId)
            },
            success: null,
            error: null
        });
    } catch (err) {
        console.error('Erro na extração:', err.message);
        res.render('index', { success: null, error: `Erro na extração: ${err.message}` });
    }
});

// Endpoint para receber ofertas diretamente da aplicação web_scraping
app.post('/api/post-deal', requireApiToken, async (req, res) => {
    try {
        const {
            titulo,
            preco,
            preco_original,
            cupom,
            destaque,
            link,
            imagem_url,
            plataforma,
            sendTelegram = true,
            sendWhatsApp = true,
            whatsappGroup
        } = req.body;

        if (!titulo || !link) {
            return res.status(400).json({ success: false, error: 'Título e Link do produto são obrigatórios.' });
        }

        const credentials = await resolveCredentials();
        const { botToken, telegramChatId, settings } = credentials;
        const whatsappGroupId = whatsappGroup || credentials.whatsappGroupId;

        // Link de afiliado conforme a configuração da loja (ver afiliados.js).
        // Sem nada configurado, o link do produto segue intacto — vale para as
        // cinco lojas da busca, não só para Amazon e Mercado Livre.
        const affiliateUrl = construirLink(plataforma, link, settings);

        // Formata preços
        const priceStr = typeof preco === 'number' ? `R$ ${preco.toLocaleString('pt-BR', {minimumFractionDigits: 2})}` : (preco || 'Confira');
        const origPriceStr = typeof preco_original === 'number' ? `R$ ${preco_original.toLocaleString('pt-BR', {minimumFractionDigits: 2})}` : preco_original;

        let priceText = `💸 <b>Por apenas: ${priceStr}</b>`;
        if (origPriceStr && origPriceStr !== priceStr) {
            priceText = `📉 <b>BAIXOU DE PREÇO!</b>\n❌ De: <s>${origPriceStr}</s>\n✅ <b>Por: ${priceStr}</b>`;
        }

        // Destaque
        let destaqueText = destaque ? `🔥 <b>${destaque.toUpperCase()}!</b>\n\n` : `🔥 <b>OFERTA IMPERDÍVEL!</b>\n\n`;

        // Cupom
        let couponText = cupom ? `\n🎟️ <b>Use o Cupom:</b> <code>${cupom}</code>\n` : '';

        const safeTitle = titulo.replace(/[<>]/g, '');
        const platTag = rotuloDaPlataforma(plataforma, link);

        // Legenda em HTML
        let caption = req.body.caption || `${destaqueText}🏷️ <b>${platTag}</b>\n📦 ${safeTitle}\n\n${priceText}\n${couponText}\n🛒 <b>Compre aqui:</b> <a href="${affiliateUrl}">Link do Desconto</a>`;

        let results = [];
        let errors = [];

        // 1. Disparo para o Telegram
        if (sendTelegram && telegramReady(botToken, telegramChatId)) {
            try {
                const telegramUrl = `https://api.telegram.org/bot${botToken}/sendPhoto`;
                if (imagem_url) {
                    await axios.post(telegramUrl, {
                        chat_id: telegramChatId,
                        photo: imagem_url,
                        caption: caption,
                        parse_mode: 'HTML'
                    });
                } else {
                    const msgUrl = `https://api.telegram.org/bot${botToken}/sendMessage`;
                    await axios.post(msgUrl, {
                        chat_id: telegramChatId,
                        text: caption,
                        parse_mode: 'HTML'
                    });
                }
                results.push('Telegram');
            } catch (err) {
                console.error('Erro Telegram:', err.message);
                errors.push(`Telegram (${err.message})`);
            }
        }

        // 2. Disparo para o WhatsApp
        if (sendWhatsApp && isWhatsAppConnected && whatsappGroupId) {
            try {
                const whatsappText = await htmlToWhatsApp(caption);
                let jid = whatsappGroupId;
                if (!jid.includes('@')) {
                    jid = jid.length > 15 ? `${jid}@g.us` : `${jid}@s.whatsapp.net`;
                }

                if (imagem_url) {
                    await sock.sendMessage(jid, { image: { url: imagem_url }, caption: whatsappText });
                } else {
                    await sock.sendMessage(jid, { text: whatsappText });
                }
                results.push('WhatsApp');
            } catch (err) {
                console.error('Erro WhatsApp:', err.message);
                errors.push(`WhatsApp (${err.message})`);
            }
        }

        return res.json({
            success: results.length > 0,
            channels: results,
            errors: errors,
            message: results.length > 0 ? `Postado com sucesso em: ${results.join(', ')}` : (errors.length > 0 ? errors.join('; ') : 'Nenhum canal ativo ou configurado.')
        });

    } catch (err) {
        console.error('Erro no /api/post-deal:', err.message);
        return res.status(500).json({ success: false, error: err.message });
    }
});

app.post('/api/confirm-send', requireAuth, async (req, res) => {
    const { photo, caption, sendTelegram, sendWhatsApp, whatsappGroup } = req.body;

    let results = [];
    let errors = [];

    try {
        const credentials = await resolveCredentials();
        const { botToken, telegramChatId } = credentials;
        const whatsappGroupId = whatsappGroup || credentials.whatsappGroupId;

        // Envio para o Telegram (se marcado)
        if (sendTelegram === 'on' || !sendWhatsApp) { // Default ou marcado
            if (telegramReady(botToken, telegramChatId)) {
                const telegramUrl = `https://api.telegram.org/bot${botToken}/sendPhoto`;
                
                if (photo.startsWith('data:image/')) {
                    // É um base64 (gerado na arte do cupom)
                    const base64Data = photo.replace(/^data:image\/\w+;base64,/, "");
                    const buffer = Buffer.from(base64Data, 'base64');
                    const form = new FormData();
                    form.append('chat_id', telegramChatId);
                    form.append('caption', caption);
                    form.append('parse_mode', 'HTML');
                    form.append('photo', buffer, { filename: 'coupon-art.jpg', contentType: 'image/jpeg' });
                    
                    await axios.post(telegramUrl, form, { headers: form.getHeaders() });
                } else {
                    // É uma URL externa comum (gerado pelo Scraper)
                    await axios.post(telegramUrl, {
                        chat_id: telegramChatId,
                        photo: photo,
                        caption: caption,
                        parse_mode: 'HTML'
                    });
                }
                results.push('Telegram');
            } else {
                errors.push('Telegram ignorado (Faltam credenciais no .env)');
            }
        }

        // Envio para o WhatsApp (se marcado)
        if (sendWhatsApp === 'on') {
            if (isWhatsAppConnected && whatsappGroupId) {
                // Formata o texto HTML para o padrão do WhatsApp (agora é async)
                const whatsappText = await htmlToWhatsApp(caption);
                
                // Formatar o ID do Grupo (o Baileys exige o sufixo @g.us ou @s.whatsapp.net)
                let jid = whatsappGroupId;
                if (!jid.includes('@')) {
                    jid = jid.length > 15 ? `${jid}@g.us` : `${jid}@s.whatsapp.net`;
                }

                if (photo.startsWith('data:image/')) {
                    // Modo Cupom (Arte Base64)
                    const base64Data = photo.replace(/^data:image\/\w+;base64,/, "");
                    const buffer = Buffer.from(base64Data, 'base64');
                    
                    await sock.sendMessage(jid, { 
                        image: buffer, 
                        caption: whatsappText 
                    });
                } else {
                    // Modo Padrão (URL)
                    await sock.sendMessage(jid, { 
                        image: { url: photo }, 
                        caption: whatsappText 
                    });
                }
                
                results.push('WhatsApp');
            } else {
                errors.push('WhatsApp ignorado (Bot não conectado ou ID do grupo ausente)');
            }
        }

        const successMsg = results.length > 0 ? `Promocao enviada para: ${results.join(', ')}!` : 'Nada enviado.';
        const errorMsg = errors.length > 0 ? `Avisos: ${errors.join(' | ')}` : null;

        res.render('index', { success: successMsg, error: errorMsg });
    } catch (err) {
        console.error('Erro na postagem:', err.message);
        res.render('index', { success: null, error: 'Ocorreu um erro ao enviar. Verifique se as tags HTML são válidas ou se o bot do WhatsApp está conectado.' });
    }
});

// Função para encurtar links via TinyURL (para o WhatsApp ficar limpo)
async function shortenUrl(url) {
    try {
        const response = await axios.get(`https://tinyurl.com/api-create.php?url=${encodeURIComponent(url)}`);
        return response.data;
    } catch (e) {
        return url; // Retorna original se falhar
    }
}

// Helper para converter HTML para formatação do WhatsApp
async function htmlToWhatsApp(html) {
    if (!html) return '';
    
    // 1. Extrai o link original do <a> para encurtar
    let finalHtml = html;
    const linkMatch = html.match(/<a href="(.*?)">.*?<\/a>/);
    if (linkMatch && linkMatch[1]) {
        const short = await shortenUrl(linkMatch[1]);
        finalHtml = html.replace(/<a href="(.*?)">(.*?)<\/a>/g, `$2:\n${short}`);
    }

    return finalHtml
        .replace(/<b>(.*?)<\/b>/g, '*$1*')        // Negrito
        .replace(/<strong>(.*?)<\/strong>/g, '*$1*')
        .replace(/<s>(.*?)<\/s>/g, '~$1~')         // Riscado
        .replace(/<i>(.*?)<\/i>/g, '_$1_')         // Itálico
        .replace(/<em>(.*?)<\/em>/g, '_$1_')
        .replace(/<code>(.*?)<\/code>/g, '```$1```') // Código (Mono)
        .replace(/<br\s*\/?>/g, '\n')              // Quebras de linha
        .replace(/<.*?>/g, '');                    // Limpa qualquer outra tag restando
}

app.listen(PORT, BIND_HOST, () => {
    console.log(`Servidor rodando em http://localhost:${PORT} (escutando em ${BIND_HOST})`);
});
