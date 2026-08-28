/**
 * Transporte vazio: WhatsApp desligado (WA_TRANSPORTE=nenhum).
 *
 * Serve para rodar só no Telegram — que é oficial, gratuito e não bane — sem
 * carregar o Baileys nem manter uma sessão de WhatsApp pareada. Também é o que
 * torna testável o resto do servidor sem depender de rede.
 */

function criar() {
    return {
        nome: 'nenhum',
        iniciar() {
            console.log('--- WhatsApp desligado (WA_TRANSPORTE=nenhum). Só o Telegram publica. ---');
        },
        get conectado() {
            return false;
        },
        get qrUrl() {
            return null;
        },
        async enviar() {
            throw new Error('WhatsApp desligado nesta instalação (WA_TRANSPORTE=nenhum).');
        },
        async listarGrupos() {
            return [];
        },
        async encerrar() {}
    };
}

module.exports = { criar };
