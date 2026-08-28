/**
 * Escolha do transporte de WhatsApp.
 *
 * O motivo de existir: `sock.sendMessage` estava escrito na mão dentro de duas
 * rotas do server.js, junto da montagem do JID. Trocar de provedor — ou
 * desligar o WhatsApp e ficar só no Telegram — significava mexer no meio da
 * lógica de publicação. Agora é uma variável de ambiente.
 *
 *   WA_TRANSPORTE=baileys   (padrão) sessão dentro deste processo
 *   WA_TRANSPORTE=http      gateway externo (Evolution API, WAHA, Z-API…)
 *   WA_TRANSPORTE=nenhum    desliga o WhatsApp; o Telegram continua normal
 *
 * Vale a nota que a análise deixou registrada: a API oficial da Meta (Cloud
 * API) NÃO envia mensagem para grupo, então ela não entra aqui como opção —
 * não é questão de preço, é de capacidade. Um adaptador para ela só faria
 * sentido se a base virasse uma lista de assinantes com opt-in.
 */

const nulo = require('./nulo');

function criarTransporte(dependencias) {
    const escolhido = (process.env.WA_TRANSPORTE || 'baileys').trim().toLowerCase();

    if (escolhido === 'nenhum' || escolhido === 'off' || escolhido === 'desligado') {
        return nulo.criar();
    }

    if (escolhido === 'http' || escolhido === 'gateway') {
        return require('./http').criar(dependencias);
    }

    if (escolhido !== 'baileys') {
        console.warn(`⚠️  WA_TRANSPORTE="${escolhido}" desconhecido — usando baileys.`);
    }

    return require('./baileys').criar(dependencias);
}

module.exports = { criarTransporte };
