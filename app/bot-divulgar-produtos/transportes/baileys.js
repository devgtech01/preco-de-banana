/**
 * Transporte WhatsApp via Baileys, rodando dentro deste processo.
 *
 * É o padrão e o único que funciona no modo portátil (pendrive, Windows sem
 * admin, sem Docker). Toda a conversa com o Baileys está presa aqui: o resto
 * do servidor só conhece `conectado`, `qrUrl`, `enviar()` e `listarGrupos()`.
 */

const path = require('path');
const {
    default: makeWASocket,
    DisconnectReason,
    fetchLatestBaileysVersion,
    Browsers
} = require('@whiskeysockets/baileys');
const qrcodeTerminal = require('qrcode-terminal');
const QRCodeWeb = require('qrcode');
const { pino } = require('pino');

const { useSQLiteAuthState } = require('../auth-state');

const PASTA_ANTIGA = path.join(__dirname, '..', 'auth_info_baileys');

function criar({ db }) {
    let sock = null;
    let auth = null;
    let conectado = false;
    let qrUrl = null;
    let encerrando = false;

    async function conectar() {
        auth = await useSQLiteAuthState(db, PASTA_ANTIGA);
        const { version } = await fetchLatestBaileysVersion();

        sock = makeWASocket({
            version,
            auth: auth.state,
            browser: Browsers.macOS('Desktop'),
            printQRInTerminal: false,
            logger: pino({ level: 'silent' }),

            // Este bot só publica: nunca lê conversa, nunca responde. Sem esses
            // ajustes o Baileys baixa e tenta descriptografar todo o histórico
            // e cada mensagem nova dos grupos — que é de onde vinha a enxurrada
            // de "Bad MAC" no log, além de I/O e CPU gastos à toa no pendrive.
            syncFullHistory: false,
            markOnlineOnConnect: false,
            shouldSyncHistoryMessage: () => false,
            // Sem store de mensagens não há como reenviar; devolver undefined é
            // a resposta correta e evita que o Baileys fique tentando.
            getMessage: async () => undefined
        });

        sock.ev.on('creds.update', auth.saveCreds);

        sock.ev.on('connection.update', (update) => {
            const { connection, lastDisconnect, qr } = update;

            if (qr) {
                console.log('\n--- LEIA O QR CODE ABAIXO PARA CONECTAR O WHATSAPP ---');
                qrcodeTerminal.generate(qr, { small: true });
                QRCodeWeb.toDataURL(qr, (err, url) => {
                    if (!err) qrUrl = url;
                });
            }

            if (connection === 'close') {
                conectado = false;
                qrUrl = null;
                if (encerrando) return;

                const deslogado = lastDisconnect?.error?.output?.statusCode === DisconnectReason.loggedOut;
                console.log(`--- WHATSAPP DESCONECTADO (deslogado do celular: ${deslogado}) ---`);

                if (deslogado) {
                    // Sessão morta: limpar antes de reconectar, senão o Baileys
                    // reapresenta credencial inválida e cai em laço.
                    Promise.resolve(auth && auth.limpar())
                        .catch(err => console.error('Falha ao limpar a sessão:', err.message))
                        .finally(() => agendar(3000));
                } else {
                    agendar(3000);
                }
            } else if (connection === 'open') {
                console.log('--- WHATSAPP CONECTADO ---');
                conectado = true;
                qrUrl = null;
            }
        });
    }

    /**
     * Reagenda a conexão tratando a rejeição da promise.
     *
     * Chamar conectar() "solto" transformava qualquer falha (rede caída na
     * subida, por exemplo) em unhandled rejection — e o Node encerra o
     * processo nesse caso.
     */
    function agendar(espera) {
        if (encerrando) return;
        setTimeout(() => {
            conectar().catch(err => {
                console.error('Falha ao conectar no WhatsApp:', err.message);
                agendar(Math.min(espera * 2, 60000));
            });
        }, espera);
    }

    return {
        nome: 'baileys',

        iniciar() {
            agendar(0);
        },

        get conectado() {
            return conectado;
        },

        get qrUrl() {
            return qrUrl;
        },

        async enviar({ destino, texto, imagemUrl, imagemBuffer }) {
            if (!conectado || !sock) throw new Error('WhatsApp não está conectado.');
            if (!destino) throw new Error('Destino do WhatsApp não configurado.');

            if (imagemBuffer) {
                return sock.sendMessage(destino, { image: imagemBuffer, caption: texto });
            }
            if (imagemUrl) {
                return sock.sendMessage(destino, { image: { url: imagemUrl }, caption: texto });
            }
            return sock.sendMessage(destino, { text: texto });
        },

        /**
         * Grupos em que a conta participa, para a tela oferecer uma lista em
         * vez de pedir que o usuário descubra e cole o JID na mão.
         */
        async listarGrupos() {
            if (!conectado || !sock) return [];
            const grupos = await sock.groupFetchAllParticipating();
            return Object.values(grupos || {})
                .map(g => ({ id: g.id, nome: g.subject || g.id, participantes: (g.participants || []).length }))
                .sort((a, b) => a.nome.localeCompare(b.nome, 'pt-BR'));
        },

        async encerrar() {
            encerrando = true;
            try {
                if (sock) sock.end(undefined);
            } catch (e) { /* já estava fechado */ }
        }
    };
}

module.exports = { criar };
