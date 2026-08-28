/**
 * Filtro do barulho do libsignal.
 *
 * O `logger: pino({ level: 'silent' })` silencia o Baileys, mas não o
 * libsignal: ele escreve direto no `console`. O resultado, conferido no
 * logs/bot.log desta instalação, é o despejo de objetos `SessionEntry`
 * INTEIROS — incluindo `privKey` e `rootKey` em hexadecimal. Ou seja: as
 * chaves privadas da sessão do WhatsApp gravadas em texto puro num arquivo de
 * log que mora no pendrive, do lado do resto.
 *
 * Este módulo precisa ser carregado ANTES do Baileys. Ele não some com a
 * informação: conta o que descartou e resume de tempos em tempos, para que um
 * "Bad MAC" em rajada continue visível como sintoma sem vazar material
 * criptográfico.
 */

// Marcas do ruído conhecido do libsignal.
const RUIDO = [
    'Closing session',
    'Closing open session',
    'Failed to decrypt message',
    'Session error',
    'SessionEntry',
    'Bad MAC',
    'No session found to decrypt'
];

// Qualquer coisa que cheire a material de chave é descartada por segurança,
// mesmo que a mensagem em si não esteja na lista acima.
const SEGREDO = ['privKey', 'chainKey', 'rootKey', 'ephemeralKeyPair', 'signedKeyPair'];

const INTERVALO_RESUMO_MS = 60_000;

function achatar(args) {
    return args.map(a => {
        if (typeof a === 'string') return a;
        try {
            return require('util').inspect(a, { depth: 3, breakLength: Infinity });
        } catch (e) {
            return String(a);
        }
    }).join(' ');
}

function instalar() {
    const contagem = new Map();
    let ultimoResumo = Date.now();

    function resumir(escrever) {
        if (!contagem.size) return;
        if (Date.now() - ultimoResumo < INTERVALO_RESUMO_MS) return;

        const partes = [...contagem.entries()].map(([marca, n]) => `${marca} x${n}`);
        contagem.clear();
        ultimoResumo = Date.now();
        escrever(`[sessão WhatsApp] ruído do libsignal suprimido: ${partes.join(', ')}`);
    }

    function filtrar(original) {
        return function (...args) {
            const linha = achatar(args);

            const marca = RUIDO.find(m => linha.includes(m));
            const temSegredo = SEGREDO.some(s => linha.includes(s));

            if (marca || temSegredo) {
                const rotulo = marca || 'material de chave';
                contagem.set(rotulo, (contagem.get(rotulo) || 0) + 1);
                resumir(msg => original.call(console, msg));
                return;
            }

            original.apply(console, args);
        };
    }

    console.log = filtrar(console.log);
    console.error = filtrar(console.error);
    console.warn = filtrar(console.warn);
    console.info = filtrar(console.info);
}

module.exports = { instalar };
