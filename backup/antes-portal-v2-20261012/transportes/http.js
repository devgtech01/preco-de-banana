/**
 * Transporte WhatsApp via gateway HTTP (Evolution API, WAHA, Z-API, UltraMsg…).
 *
 * Não é o padrão e não serve ao modo pendrive — todos esses gateways querem
 * Docker ou uma assinatura. Existe para o dia em que este bot rodar numa VPS
 * pelo Dockerfile que já está no projeto: aí a sessão do WhatsApp passa a viver
 * fora deste processo, sobrevive a um restart do bot e deixa de ser o motivo de
 * "uma cópia por vez".
 *
 * Ligar com:
 *   WA_TRANSPORTE=http
 *   WA_HTTP_URL=http://localhost:8080/message/sendText/minhainstancia
 *   WA_HTTP_URL_IMAGEM=http://localhost:8080/message/sendMedia/minhainstancia
 *   WA_HTTP_TOKEN=...            (vai no header, ver WA_HTTP_HEADER_TOKEN)
 *   WA_HTTP_HEADER_TOKEN=apikey  (Evolution usa "apikey"; Z-API usa outro)
 *
 * O corpo é montado no formato da Evolution API, que é o mais comum por aqui.
 * Gateway com contrato diferente troca o mapeamento em `montarCorpo`.
 */

const axios = require('axios');

function criar() {
    const urlTexto = (process.env.WA_HTTP_URL || '').trim();
    const urlImagem = (process.env.WA_HTTP_URL_IMAGEM || '').trim() || urlTexto;
    const token = (process.env.WA_HTTP_TOKEN || '').trim();
    const headerToken = (process.env.WA_HTTP_HEADER_TOKEN || 'apikey').trim();
    const urlGrupos = (process.env.WA_HTTP_URL_GRUPOS || '').trim();

    if (!urlTexto) {
        console.warn('⚠️  WA_TRANSPORTE=http, mas WA_HTTP_URL não foi definida — o WhatsApp ficará inativo.');
    }

    function cabecalhos() {
        const h = { 'Content-Type': 'application/json' };
        if (token) h[headerToken] = token;
        return h;
    }

    function montarCorpo({ destino, texto, imagemUrl, imagemBuffer }) {
        if (imagemBuffer) {
            return {
                number: destino,
                mediatype: 'image',
                caption: texto,
                media: imagemBuffer.toString('base64')
            };
        }
        if (imagemUrl) {
            return { number: destino, mediatype: 'image', caption: texto, media: imagemUrl };
        }
        return { number: destino, text: texto };
    }

    return {
        nome: 'http',

        iniciar() {
            if (urlTexto) console.log(`--- WhatsApp via gateway HTTP: ${urlTexto} ---`);
        },

        // O gateway é quem mantém a sessão; daqui só dá para dizer que está
        // configurado. Um gateway fora do ar aparece como erro no envio.
        get conectado() {
            return !!urlTexto;
        },

        // O QR Code vive no painel do próprio gateway.
        get qrUrl() {
            return null;
        },

        async enviar(mensagem) {
            if (!urlTexto) throw new Error('WA_HTTP_URL não configurada.');
            if (!mensagem.destino) throw new Error('Destino do WhatsApp não configurado.');

            const alvo = (mensagem.imagemUrl || mensagem.imagemBuffer) ? urlImagem : urlTexto;
            const resposta = await axios.post(alvo, montarCorpo(mensagem), {
                headers: cabecalhos(),
                timeout: 30000
            });
            return resposta.data;
        },

        async listarGrupos() {
            if (!urlGrupos) return [];
            try {
                const resposta = await axios.get(urlGrupos, { headers: cabecalhos(), timeout: 15000 });
                const bruto = Array.isArray(resposta.data) ? resposta.data : (resposta.data?.groups || []);
                return bruto.map(g => ({
                    id: g.id || g.jid || '',
                    nome: g.subject || g.name || g.id || '',
                    participantes: (g.participants || []).length
                })).filter(g => g.id);
            } catch (err) {
                console.error('Falha ao listar grupos no gateway:', err.message);
                return [];
            }
        },

        async encerrar() { /* nada a fechar: a sessão não é nossa */ }
    };
}

module.exports = { criar };
