const fs = require('fs');
const sqlite3 = require('sqlite3').verbose();
const path = require('path');
const { camposDeAfiliado } = require('./afiliados');

// O banco vive em ./data para que um único volume do Docker preserve as
// configurações. Antes o settings.db ficava na raiz do projeto, era copiado
// para dentro da imagem pelo `COPY . .` e não tinha volume nenhum: tudo que
// fosse salvo na tela de afiliados sumia no próximo build.
const dataDir = process.env.DATA_DIR || path.join(__dirname, 'data');
fs.mkdirSync(dataDir, { recursive: true });

const dbPath = path.join(dataDir, 'settings.db');

// Se um volume mal configurado criou um diretório no lugar do arquivo, remove.
if (fs.existsSync(dbPath) && fs.statSync(dbPath).isDirectory()) {
    try {
        fs.rmSync(dbPath, { recursive: true, force: true });
    } catch (e) {
        console.error('Não foi possível remover o diretório inválido settings.db:', e.message);
    }
}

// Migração automática do banco antigo (raiz do projeto) para ./data.
const legacyPath = path.join(__dirname, 'settings.db');
if (!fs.existsSync(dbPath) && fs.existsSync(legacyPath) && fs.statSync(legacyPath).isFile()) {
    try {
        fs.copyFileSync(legacyPath, dbPath);
        console.log('Configurações migradas de settings.db para data/settings.db');
    } catch (e) {
        console.error('Falha ao migrar o banco antigo:', e.message);
    }
}

const db = new sqlite3.Database(dbPath);

// Colunas adicionadas depois da criação original da tabela.
const EXTRA_COLUMNS = [...new Set([
    'aliApiKey', 'aliApiSecret', 'aliTrackingId',
    'shopeeAppKey', 'shopeeAppSecret',
    // Credenciais de disparo: ficam no banco (com volume) e não mais num .env
    // gravado dentro do container, que era perdido a cada recriação.
    'telegramBotToken', 'telegramChatId', 'whatsappGroupId',
    // Campos de afiliado de todas as lojas. A lista sai de afiliados.js, que é
    // a fonte da verdade: registrar uma loja lá cria as colunas aqui sozinho,
    // sem risco de o banco ficar sem uma coluna que o código já grava.
    ...camposDeAfiliado()
])];

db.serialize(() => {
    db.run(`CREATE TABLE IF NOT EXISTS settings (
        id INTEGER PRIMARY KEY DEFAULT 1,
        amazonTag TEXT DEFAULT '',
        aliApiKey TEXT DEFAULT '',
        aliApiSecret TEXT DEFAULT '',
        aliTrackingId TEXT DEFAULT '',
        shopeeAppKey TEXT DEFAULT '',
        shopeeAppSecret TEXT DEFAULT '',
        mlAffiliateLink TEXT DEFAULT '',
        magaluAffiliateLink TEXT DEFAULT '',
        telegramBotToken TEXT DEFAULT '',
        telegramChatId TEXT DEFAULT '',
        whatsappGroupId TEXT DEFAULT ''
    )`);

    // Roda sempre: bancos criados por versões antigas não têm as colunas novas.
    // O erro "duplicate column name" é esperado e ignorado.
    EXTRA_COLUMNS.forEach(col => {
        db.run(`ALTER TABLE settings ADD COLUMN ${col} TEXT DEFAULT ''`, () => {});
    });

    db.get('SELECT id FROM settings WHERE id = 1', (err, row) => {
        if (!row) {
            db.run("INSERT INTO settings (id) VALUES (1)");
        }
    });
});

function getSettings() {
    return new Promise((resolve, reject) => {
        db.get('SELECT * FROM settings WHERE id = 1', (err, row) => {
            if (err) reject(err);
            else resolve(row || {});
        });
    });
}

const UPDATABLE = [...new Set([
    'aliApiKey', 'aliApiSecret', 'aliTrackingId',
    'shopeeAppKey', 'shopeeAppSecret',
    'telegramBotToken', 'telegramChatId', 'whatsappGroupId',
    ...camposDeAfiliado()
])];

function updateSettings(settings) {
    return new Promise((resolve, reject) => {
        // COALESCE(?, coluna) preserva o valor atual quando o campo não veio no
        // corpo da requisição — salvar só a tag da Amazon não apaga o resto.
        const assignments = UPDATABLE.map(col => `${col} = COALESCE(?, ${col})`).join(', ');
        const values = UPDATABLE.map(col => (settings[col] === undefined ? null : settings[col]));

        db.run(
            `UPDATE settings SET ${assignments} WHERE id = 1`,
            values,
            function (err) {
                if (err) reject(err);
                else resolve();
            }
        );
    });
}

module.exports = { db, getSettings, updateSettings };
