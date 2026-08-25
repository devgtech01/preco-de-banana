/**
 * Regras de link de afiliado, uma tabela para todas as lojas.
 *
 * Antes cada loja tinha o seu `if` espalhado: a Amazon ganhava a tag no
 * /api/post-deal, o Mercado Livre trocava o link inteiro, e Shopee/AliExpress
 * simplesmente não tinham tratamento. Com cinco marketplaces isso vira código
 * duplicado em três arquivos, então as regras moram aqui e cada lugar só chama
 * `construirLink`.
 *
 * Cada loja aceita três formas de configuração, aplicadas nesta ordem:
 *
 *   1. MODELO (campo `*AffiliateTemplate`) contendo `{url}` — vira um deeplink:
 *      o endereço do produto entra codificado no lugar do `{url}`. É o formato
 *      de rede de afiliados (Awin, Rakuten, s.click do AliExpress).
 *   2. MODELO sem `{url}` ou LINK FIXO (`mlAffiliateLink`, `magaluAffiliateLink`)
 *      — substitui o endereço do produto. É como o Mercado Livre já funcionava.
 *   3. TAG (`amazonTag`) — entra como parâmetro na própria URL do produto.
 *
 * Em qualquer um dos casos, `*AffiliateParams` é anexado no fim (sub-id,
 * utm_source, o que a rede pedir). Sem nada configurado o link do produto sai
 * limpo, exatamente como veio da busca — é o padrão até o usuário se afiliar.
 */

const MARKETPLACES = {
    amazon: {
        rotulo: '📦 Amazon Brasil',
        termos: ['amazon', 'amzn'],
        dominios: ['amazon.', 'amzn.to'],
        home: 'https://www.amazon.com.br/',
        campoTag: 'amazonTag',
        parametroTag: 'tag',
        campoModelo: 'amazonAffiliateTemplate',
        campoParametros: 'amazonAffiliateParams'
    },
    mercadolivre: {
        rotulo: '🛒 Mercado Livre',
        termos: ['mercado livre', 'mercadolivre', 'mercadolibre', 'meli'],
        dominios: ['mercadolivre.com', 'mercadolibre.com', 'meli.la', 'merca.li'],
        home: 'https://www.mercadolivre.com.br/',
        campoLink: 'mlAffiliateLink',
        campoModelo: 'mlAffiliateTemplate',
        campoParametros: 'mlAffiliateParams'
    },
    shopee: {
        rotulo: '🧡 Shopee',
        termos: ['shopee'],
        dominios: ['shopee.', 'shp.ee'],
        home: 'https://shopee.com.br/',
        campoModelo: 'shopeeAffiliateTemplate',
        campoParametros: 'shopeeAffiliateParams'
    },
    aliexpress: {
        rotulo: '🌐 AliExpress',
        termos: ['aliexpress', 'ali express'],
        dominios: ['aliexpress.com', 'ali.ski'],
        home: 'https://pt.aliexpress.com/',
        campoModelo: 'aliAffiliateTemplate',
        campoParametros: 'aliAffiliateParams'
    },
    kabum: {
        rotulo: '💙 KaBuM!',
        termos: ['kabum'],
        dominios: ['kabum.com.br'],
        home: 'https://www.kabum.com.br/',
        campoModelo: 'kabumAffiliateTemplate',
        campoParametros: 'kabumAffiliateParams'
    },
    magalu: {
        rotulo: '🔷 Magalu',
        termos: ['magalu', 'magazine'],
        dominios: ['magazineluiza.com.br', 'magalu.com', 'magazinevoce.com.br'],
        home: 'https://www.magazineluiza.com.br/',
        campoLink: 'magaluAffiliateLink',
        campoModelo: 'magaluAffiliateTemplate',
        campoParametros: 'magaluAffiliateParams'
    }
};

const ROTULO_PADRAO = '🛍️ Oferta';

function texto(valor) {
    return String(valor === undefined || valor === null ? '' : valor).trim();
}

function campo(settings, nome) {
    return nome ? texto((settings || {})[nome]) : '';
}

/** Descobre a loja pelo nome da plataforma e, se não bastar, pelo domínio do link. */
function identificar(plataforma, url) {
    const nome = texto(plataforma).toLowerCase();
    const endereco = texto(url).toLowerCase();

    for (const [chave, info] of Object.entries(MARKETPLACES)) {
        if (nome && info.termos.some(termo => nome.includes(termo))) {
            return { chave, info };
        }
    }

    for (const [chave, info] of Object.entries(MARKETPLACES)) {
        if (endereco && info.dominios.some(dominio => endereco.includes(dominio))) {
            return { chave, info };
        }
    }

    return null;
}

/** Anexa parâmetros extras ("sub_id=grupo&utm_source=whatsapp") a uma URL. */
function comParametros(url, extras) {
    const consulta = texto(extras).replace(/^[?&]+/, '');
    if (!consulta) return url;

    try {
        const parsed = new URL(url);
        for (const [chave, valor] of new URLSearchParams(consulta)) {
            parsed.searchParams.set(chave, valor);
        }
        return parsed.toString();
    } catch (e) {
        // Modelos de deeplink às vezes não são URLs válidas para o parser
        // (chaves duplicadas, placeholder sobrando). Concatenar ainda funciona.
        return url + (url.includes('?') ? '&' : '?') + consulta;
    }
}

function comTag(url, parametro, valor) {
    try {
        const parsed = new URL(url);
        parsed.searchParams.set(parametro, valor);
        return parsed.toString();
    } catch (e) {
        return url + (url.includes('?') ? '&' : '?') + `${parametro}=${encodeURIComponent(valor)}`;
    }
}

/**
 * Devolve o link que deve ir para o grupo.
 * Sem configuração de afiliado, devolve o link original intacto.
 *
 * `opcoes.permitirLinkFixo = false` desliga a substituição do endereço: quem
 * já tem a página de um produto específico em mãos quer divulgar aquele
 * produto, não a home da loja. Deeplinks com {url}, tag e parâmetros extras
 * continuam sendo aplicados nesse modo.
 */
function construirLink(plataforma, url, settings = {}, opcoes = {}) {
    const alvo = texto(url);
    if (!alvo) return alvo;

    const encontrado = identificar(plataforma, alvo);
    if (!encontrado) return alvo;

    const { info } = encontrado;
    let final = alvo;

    const modelo = campo(settings, info.campoModelo);
    const linkFixo = campo(settings, info.campoLink);
    const tag = campo(settings, info.campoTag);
    const podeSubstituir = opcoes.permitirLinkFixo !== false;

    if (modelo && modelo.includes('{url}')) {
        final = modelo.replace(/\{url\}/g, encodeURIComponent(alvo));
    } else if (modelo && podeSubstituir) {
        final = modelo;
    } else if (linkFixo && podeSubstituir) {
        final = linkFixo;
    } else if (tag && info.parametroTag) {
        // A tag só faz sentido no domínio da própria loja: um link encurtado de
        // terceiro ganharia um parâmetro inútil.
        const noDominio = info.dominios.some(dominio => alvo.toLowerCase().includes(dominio));
        if (noDominio) final = comTag(final, info.parametroTag, tag);
    }

    return comParametros(final, campo(settings, info.campoParametros));
}

/** Rótulo com emoji usado na legenda da promoção. */
function rotuloDaPlataforma(plataforma, url) {
    const encontrado = identificar(plataforma, url);
    return encontrado ? encontrado.info.rotulo : ROTULO_PADRAO;
}

/**
 * Link de entrada da loja com afiliação aplicada.
 * Usado pela arte de cupom, que divulga a loja e não um produto específico.
 */
function linkDaLoja(nomeDaLoja, settings = {}) {
    const encontrado = identificar(nomeDaLoja, '');
    if (!encontrado) return 'https://seusite.com.br';
    return construirLink(nomeDaLoja, encontrado.info.home, settings);
}

/** Colunas que o banco precisa ter para guardar tudo isso. */
function camposDeAfiliado() {
    const campos = [];
    for (const info of Object.values(MARKETPLACES)) {
        for (const nome of [info.campoTag, info.campoLink, info.campoModelo, info.campoParametros]) {
            if (nome && !campos.includes(nome)) campos.push(nome);
        }
    }
    return campos;
}

module.exports = {
    MARKETPLACES,
    construirLink,
    rotuloDaPlataforma,
    linkDaLoja,
    camposDeAfiliado,
    identificar
};
