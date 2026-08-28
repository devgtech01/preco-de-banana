/**
 * Estado de autenticação do WhatsApp guardado no SQLite.
 *
 * Por que trocar o `useMultiFileAuthState`: ele grava um arquivo por chave do
 * Signal. Nesta instalação a pasta `auth_info_baileys/` chegou a 1297 arquivos.
 * Em pendrive isso é o pior caso possível — milhares de escritas pequenas numa
 * mídia lenta, e é exatamente o conjunto de arquivos que fica corrompido
 * quando alguém arranca o pendrive sem rodar o PARAR.bat (o LEIA-ME já avisa
 * disso). O `settings.db` já é o arquivo que precisa sobreviver; colocar a
 * sessão dentro dele reduz o estrago a um arquivo só, com as garantias de
 * transação do SQLite.
 *
 * A migração é automática e acontece uma vez: se a tabela estiver vazia e a
 * pasta antiga existir, os arquivos são importados e a sessão continua de pé —
 * ninguém precisa ler o QR Code de novo. A pasta NÃO é apagada; ela vira o
 * backup natural caso algo dê errado na primeira subida.
 */

const fs = require('fs');
const path = require('path');
const { proto, initAuthCreds, BufferJSON } = require('@whiskeysockets/baileys');

// Registro de controle: diz que a importação da pasta antiga já aconteceu.
const MARCADOR = '__migrado_de_arquivos__';

/** Mesma normalização de nome que o Baileys usa nos arquivos. */
function nomeSeguro(nome) {
    return String(nome || '').replace(/\//g, '__').replace(/:/g, '-');
}

function executar(db, sql, params = []) {
    return new Promise((resolve, reject) => {
        db.run(sql, params, function (err) {
            if (err) reject(err);
            else resolve(this);
        });
    });
}

function buscarUma(db, sql, params = []) {
    return new Promise((resolve, reject) => {
        db.get(sql, params, (err, row) => (err ? reject(err) : resolve(row)));
    });
}

function buscarTodas(db, sql, params = []) {
    return new Promise((resolve, reject) => {
        db.all(sql, params, (err, rows) => (err ? reject(err) : resolve(rows || [])));
    });
}

function serializar(valor) {
    return JSON.stringify(valor, BufferJSON.replacer);
}

function desserializar(texto, tipo) {
    if (texto === undefined || texto === null) return null;
    const valor = JSON.parse(texto, BufferJSON.reviver);

    // O Baileys espera esse tipo como mensagem do protobuf, não como objeto
    // solto — é o mesmo tratamento especial que o useMultiFileAuthState faz.
    if (tipo === 'app-state-sync-key' && valor) {
        return proto.Message.AppStateSyncKeyData.fromObject(valor);
    }
    return valor;
}

/**
 * Importa `auth_info_baileys/*.json` para a tabela, uma única vez.
 * Devolve quantos registros vieram.
 */
async function migrarDosArquivos(db, pastaAntiga) {
    if (!pastaAntiga || !fs.existsSync(pastaAntiga)) return 0;

    let arquivos;
    try {
        arquivos = fs.readdirSync(pastaAntiga).filter(nome => nome.endsWith('.json'));
    } catch (err) {
        console.error('Não foi possível ler a sessão antiga do WhatsApp:', err.message);
        return 0;
    }
    if (!arquivos.length) return 0;

    let importados = 0;
    for (const arquivo of arquivos) {
        const chave = arquivo.replace(/\.json$/, '');
        try {
            const conteudo = fs.readFileSync(path.join(pastaAntiga, arquivo), 'utf-8');
            // Guardado como veio: a string já está no formato do BufferJSON.
            JSON.parse(conteudo, BufferJSON.reviver); // valida antes de gravar
            await executar(db, 'INSERT OR REPLACE INTO wa_auth (chave, valor) VALUES (?, ?)', [chave, conteudo]);
            importados++;
        } catch (err) {
            // Arquivo corrompido (o cenário do pendrive arrancado): pular um
            // pre-key é recuperável, o Baileys gera outro. Abortar não é.
            console.warn(`Sessão do WhatsApp: ignorando "${arquivo}" (${err.message})`);
        }
    }

    return importados;
}

/**
 * Devolve { state, saveCreds } no formato que o makeWASocket espera.
 *
 * @param db          conexão sqlite3 já aberta (a mesma do settings.db)
 * @param pastaAntiga caminho do auth_info_baileys/ para a migração inicial
 */
async function useSQLiteAuthState(db, pastaAntiga) {
    await executar(db, 'CREATE TABLE IF NOT EXISTS wa_auth (chave TEXT PRIMARY KEY, valor TEXT NOT NULL)');

    // Marcador em vez de "a tabela está vazia": depois de um logout a tabela
    // fica vazia de novo, e sem o marcador a migração ressuscitaria a sessão
    // morta a partir dos arquivos antigos, num laço de reconexão sem fim.
    const jaMigrou = await buscarUma(db, 'SELECT 1 AS ok FROM wa_auth WHERE chave = ?', [MARCADOR]);
    if (!jaMigrou) {
        const importados = await migrarDosArquivos(db, pastaAntiga);
        if (importados) {
            console.log(`Sessão do WhatsApp migrada para o banco (${importados} registros). A pasta antiga foi mantida como backup.`);
        }
        await executar(db, 'INSERT OR REPLACE INTO wa_auth (chave, valor) VALUES (?, ?)', [MARCADOR, JSON.stringify({ em: new Date().toISOString() })]);
    }

    async function ler(chave, tipo) {
        const linha = await buscarUma(db, 'SELECT valor FROM wa_auth WHERE chave = ?', [chave]);
        if (!linha) return null;
        try {
            return desserializar(linha.valor, tipo);
        } catch (err) {
            console.warn(`Sessão do WhatsApp: registro "${chave}" ilegível (${err.message}).`);
            return null;
        }
    }

    async function gravar(chave, valor) {
        await executar(db, 'INSERT OR REPLACE INTO wa_auth (chave, valor) VALUES (?, ?)', [chave, serializar(valor)]);
    }

    async function remover(chave) {
        await executar(db, 'DELETE FROM wa_auth WHERE chave = ?', [chave]);
    }

    const creds = (await ler('creds')) || initAuthCreds();

    return {
        state: {
            creds,
            keys: {
                get: async (tipo, ids) => {
                    const resultado = {};
                    for (const id of ids) {
                        const valor = await ler(nomeSeguro(`${tipo}-${id}`), tipo);
                        if (valor) resultado[id] = valor;
                    }
                    return resultado;
                },
                set: async (dados) => {
                    // Uma transação só: dezenas de chaves por vez é o normal
                    // durante o pareamento, e cada INSERT solto é um fsync.
                    await executar(db, 'BEGIN IMMEDIATE');
                    try {
                        for (const tipo of Object.keys(dados)) {
                            for (const id of Object.keys(dados[tipo])) {
                                const valor = dados[tipo][id];
                                const chave = nomeSeguro(`${tipo}-${id}`);
                                if (valor) await gravar(chave, valor);
                                else await remover(chave);
                            }
                        }
                        await executar(db, 'COMMIT');
                    } catch (err) {
                        await executar(db, 'ROLLBACK').catch(() => {});
                        throw err;
                    }
                }
            }
        },
        saveCreds: () => gravar('creds', creds),
        /**
         * Usado no logout: zera a sessão sem tocar nas configurações e sem
         * remover o marcador — a pasta antiga não pode voltar a ser importada.
         */
        limpar: () => executar(db, 'DELETE FROM wa_auth WHERE chave != ?', [MARCADOR])
    };
}

module.exports = { useSQLiteAuthState };
