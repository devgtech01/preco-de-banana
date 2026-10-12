/**
 * Fila de publicação do site (Tech Lobo) → WhatsApp.
 *
 * O site não alcança este computador (ele fica atrás do roteador de casa e
 * assim deve continuar). Então é o bot que pergunta, a cada 30 s, se há
 * publicações de WhatsApp esperando; envia cada uma pelo mesmo transporte das
 * outras telas e devolve "enviado" ou "erro" para o painel do site.
 *
 * Liga sozinho quando SITE_API_URL e SITE_API_TOKEN existem (o iniciar.ps1
 * preenche a partir de SITE_URL e SITE_TOKEN do config.txt).
 */

const axios = require('axios');

const INTERVALO_MS = 30_000;
const LOTE = 5;

function numeroDoAmbiente(nome, padrao) {
    const valor = Number(process.env[nome]);
    return Number.isFinite(valor) && valor >= 0 ? valor : padrao;
}

// Mesmo intervalo do disparo em lote do portal: mensagens em sequência rápida
// são o caminho mais curto para o WhatsApp banir o número.
const ESPERA_MIN = numeroDoAmbiente('PUBLISH_MIN_DELAY', 6) * 1000;
const ESPERA_MAX = Math.max(ESPERA_MIN, numeroDoAmbiente('PUBLISH_MAX_DELAY', 14) * 1000);

const esperar = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const esperaAleatoria = () => ESPERA_MIN + Math.random() * (ESPERA_MAX - ESPERA_MIN);

/** O htmlToWhatsApp não desfaz entidades: sem isto "&amp;" chegaria literal no grupo. */
function desfazerEntidades(texto) {
    return String(texto || '')
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>')
        .replace(/&quot;/g, '"')
        .replace(/&#(\d+);/g, (_, codigo) => String.fromCharCode(Number(codigo)))
        .replace(/&amp;/g, '&');
}

function iniciarFilaDoSite({ transporte, resolveCredentials, normalizarJid, htmlToWhatsApp }) {
    const base = String(process.env.SITE_API_URL || '').trim().replace(/\/+$/, '');
    const token = String(process.env.SITE_API_TOKEN || '').trim();

    if (!base || !token) {
        console.log('Fila do site: desligada (preencha SITE_URL e SITE_TOKEN no config.txt).');
        return { parar() {} };
    }

    const cliente = axios.create({
        baseURL: base,
        timeout: 20_000,
        headers: { Authorization: `Bearer ${token}`, 'User-Agent': 'preco-de-banana/bot' }
    });

    let ocupado = false;
    let ultimoAviso = '';

    // Só repete no log quando a situação muda: a cada 30 s isso viraria ruído.
    function avisar(mensagem) {
        if (mensagem !== ultimoAviso) {
            console.log(`Fila do site: ${mensagem}`);
            ultimoAviso = mensagem;
        }
    }

    async function responder(id, ok, erro) {
        try {
            await cliente.post(`/api/integracao/fila/${id}`, ok ? { status: 'enviado' } : { status: 'erro', erro: String(erro || '').slice(0, 480) });
        } catch (e) {
            // Sem resposta, o site devolve o pedido à fila em 10 min: ele não se perde.
            console.error(`Fila do site: não consegui confirmar o envio ${id}: ${e.message}`);
        }
    }

    async function enviar(item, destino) {
        const texto = desfazerEntidades(htmlToWhatsApp(item.legenda));
        try {
            await transporte.enviar({ destino, texto, imagemUrl: item.imagem || null });
        } catch (erroComImagem) {
            // Imagem inacessível não pode impedir a oferta de sair: vai só o texto.
            if (!item.imagem) throw erroComImagem;
            await transporte.enviar({ destino, texto, imagemUrl: null });
        }
    }

    async function ciclo() {
        if (ocupado) return;
        ocupado = true;
        try {
            const { whatsappGroupId } = await resolveCredentials();
            const conectado = !!transporte.conectado;
            const grupo = !!whatsappGroupId;

            // Avisa o site que está vivo mesmo sem poder enviar: o painel mostra o motivo.
            const { data } = await cliente.post('/api/integracao/fila', { canal: 'whatsapp', limite: LOTE, conectado, grupo });

            if (!conectado) return avisar('WhatsApp desconectado, aguardando.');
            if (!grupo) return avisar('nenhum grupo de WhatsApp configurado no bot.');

            const itens = Array.isArray(data && data.itens) ? data.itens : [];
            avisar(itens.length ? 'enviando publicações.' : 'conectado, sem publicações pendentes.');

            for (const [indice, item] of itens.entries()) {
                if (indice > 0) await esperar(esperaAleatoria());
                try {
                    await enviar(item, normalizarJid(whatsappGroupId));
                    await responder(item.id, true);
                    console.log(`Fila do site: publicação ${item.id} enviada ao WhatsApp.`);
                } catch (e) {
                    console.error(`Fila do site: falha na publicação ${item.id}: ${e.message}`);
                    await responder(item.id, false, e.message);
                }
            }
        } catch (e) {
            const status = e.response && e.response.status;
            const motivo = (e.response && e.response.data && e.response.data.erro) || e.message;
            avisar(status === 401 ? 'token recusado pelo site (confira SITE_TOKEN).' : `site indisponível (${motivo}).`);
        } finally {
            ocupado = false;
        }
    }

    console.log(`Fila do site: ligada (${base}), consultando a cada ${INTERVALO_MS / 1000} s.`);
    const primeiro = setTimeout(ciclo, 5_000);
    const timer = setInterval(ciclo, INTERVALO_MS);

    return {
        ciclo,
        parar() {
            clearTimeout(primeiro);
            clearInterval(timer);
        }
    };
}

module.exports = { iniciarFilaDoSite, desfazerEntidades };
