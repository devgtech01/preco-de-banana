/**
 * Arte dos avisos de cupom (1080x1080, JPG).
 *
 * Simples de propósito: a logo do canal ocupa a imagem inteira e o ícone da
 * loja do cupom entra num cartão no canto superior direito. Código, desconto,
 * compra mínima e validade vão na legenda, junto com o link afiliado.
 *
 * Arquivos (na pasta arte\ da raiz do programa):
 *   logo-grupo.png          logo do canal (sem ela não há arte: vai a foto comum)
 *   lojas\kabum.png         ícones das lojas. Sem o ícone, a arte sai só com a
 *   lojas\mercadolivre.png  logo do canal.
 *   lojas\amazon.png
 *   lojas\aliexpress.png
 *   lojas\shopee.png
 *   lojas\magalu.png
 *
 * Usa o sharp que já vem no node_modules do bot. Qualquer falha devolve null e
 * a postagem segue com a foto comum — a arte nunca derruba um envio.
 */

const fs = require('fs');
const path = require('path');

let sharp = null;
try {
    sharp = require('sharp');
} catch (e) {
    sharp = null;
}

const PASTA_ARTE = path.join(__dirname, '..', '..', 'arte');
const LADO = 1080;
const CARTAO = 270;     // lado do cartão com o ícone da loja
const MARGEM = 44;

const LOJAS = [
    ['kabum', ['kabum']],
    ['mercadolivre', ['mercado livre', 'mercadolivre', 'meli']],
    ['amazon', ['amazon']],
    ['aliexpress', ['aliexpress']],
    ['shopee', ['shopee']],
    ['magalu', ['magalu', 'magazine']],
];

function chaveDaLoja(nome) {
    const baixo = String(nome || '').toLowerCase();
    const achada = LOJAS.find(([, termos]) => termos.some((t) => baixo.includes(t)));
    return achada ? achada[0] : null;
}

function arquivo(...partes) {
    for (const ext of ['png', 'jpg', 'jpeg', 'webp']) {
        const caminho = path.join(PASTA_ARTE, ...partes) + '.' + ext;
        if (fs.existsSync(caminho)) return caminho;
    }
    return null;
}

/** Cor do canto do ícone: vira o fundo do cartão (branco se for transparente). */
async function corDeFundo(caminho) {
    const { data, info } = await sharp(caminho).ensureAlpha().raw().toBuffer({ resolveWithObject: true });
    const i = (2 * info.width + 2) * info.channels;
    if (data[i + 3] < 200) return { r: 255, g: 255, b: 255, alpha: 1 };
    return { r: data[i], g: data[i + 1], b: data[i + 2], alpha: 1 };
}

async function cartaoDaLoja(caminho) {
    const fundo = await corDeFundo(caminho);
    const meta = await sharp(caminho).metadata();
    const quadrado = meta.width && meta.height && Math.abs(meta.width - meta.height) / Math.max(meta.width, meta.height) < 0.08;
    // Ícone quadrado ocupa o cartão todo; logo comprida/alta fica inteira, com respiro.
    const icone = quadrado
        ? await sharp(caminho).resize(CARTAO, CARTAO, { fit: 'cover' }).png().toBuffer()
        // Em duas etapas: o sharp aplica o flatten antes do resize, então a
        // sobra do 'contain' ficaria preta se tudo fosse num pipeline só.
        : await sharp(
            await sharp(caminho)
                .resize(CARTAO - 44, CARTAO - 44, { fit: 'contain', background: { r: 0, g: 0, b: 0, alpha: 0 } })
                .png().toBuffer()
        )
            .flatten({ background: fundo })
            .extend({ top: 22, bottom: 22, left: 22, right: 22, background: fundo })
            .png().toBuffer();

    const raio = 46;
    const mascara = Buffer.from(`<svg width="${CARTAO}" height="${CARTAO}"><rect width="${CARTAO}" height="${CARTAO}" rx="${raio}" fill="#fff"/></svg>`);
    const borda = Buffer.from(`<svg width="${CARTAO}" height="${CARTAO}"><rect x="4" y="4" width="${CARTAO - 8}" height="${CARTAO - 8}" rx="${raio - 4}" fill="none" stroke="#ff7a00" stroke-width="8"/></svg>`);
    return sharp(icone)
        .resize(CARTAO, CARTAO)
        .composite([{ input: mascara, blend: 'dest-in' }, { input: borda }])
        .png().toBuffer();
}

/**
 * dados: { loja, ... }  (os demais campos ficam na legenda)
 * Devolve Buffer JPEG ou null.
 */
async function gerarArteCupom(dados) {
    if (!sharp) return null;
    try {
        const logo = arquivo('logo-grupo');
        if (!logo) return null;

        const camadas = [];
        const chave = chaveDaLoja(dados && dados.loja);
        const icone = chave ? arquivo('lojas', chave) : null;
        if (icone) {
            const cartao = await cartaoDaLoja(icone);
            // Sombra leve para o cartão destacar sobre o fundo escuro.
            const sombra = Buffer.from(`<svg width="${CARTAO + 60}" height="${CARTAO + 60}"><defs><filter id="s" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="14"/></filter></defs><rect x="30" y="38" width="${CARTAO}" height="${CARTAO}" rx="46" fill="#000" fill-opacity="0.75" filter="url(#s)"/></svg>`);
            const x = LADO - MARGEM - CARTAO;
            camadas.push({ input: sombra, left: x - 30, top: MARGEM - 30 });
            camadas.push({ input: cartao, left: x, top: MARGEM });
        }

        return await sharp(logo)
            .resize(LADO, LADO, { fit: 'cover' })
            .flatten({ background: '#000000' })
            .composite(camadas)
            .jpeg({ quality: 90 })
            .toBuffer();
    } catch (err) {
        console.error('Arte do cupom falhou:', err.message);
        return null;
    }
}

/** Gera arte\exemplo-cupom.jpg ao subir o bot, para conferir o visual. */
async function gerarExemplo() {
    try {
        if (!fs.existsSync(PASTA_ARTE)) return;
        const buffer = await gerarArteCupom({ loja: 'KaBuM!' });
        if (buffer) fs.writeFileSync(path.join(PASTA_ARTE, 'exemplo-cupom.jpg'), buffer);
    } catch (e) {
        /* só um exemplo */
    }
}

module.exports = { gerarArteCupom, gerarExemplo, PASTA_ARTE };
