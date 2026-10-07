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
 *
 * Antes de tudo isso vem uma checagem: `dominiosAfiliados` lista os endereços
 * que JÁ nascem afiliados (o `offerLink` da API da Shopee, o `s.click` do
 * AliExpress, o `/sec/` do Mercado Livre, o `magazinevoce` do Magalu).
 * Reescrever um desses é perda dupla — some o produto específico e some a
 * atribuição que já estava correta —, então eles só recebem os parâmetros
 * extras e seguem intactos.
 *
 * A lista `campos` de cada loja é a fonte da UI: tanto a tela do bot quanto o
 * painel do portal se desenham a partir dela (`/api/affiliate-schema`). Antes
 * o portal tinha os inputs escritos na mão e expunha 1 campo de 4, sem Magalu:
 * registrar uma loja aqui não bastava, era preciso lembrar de editar o HTML.
 */

const MARKETPLACES = {
    amazon: {
        rotulo: '📦 Amazon Brasil',
        nome: 'Amazon',
        termos: ['amazon', 'amzn'],
        dominios: ['amazon.', 'amzn.to'],
        // A tag só vale no domínio da loja: num encurtador ela viraria um
        // parâmetro inútil (o amzn.to já resolve para o link de alguém).
        dominiosTag: ['amazon.'],
        dominiosAfiliados: ['amzn.to'],
        home: 'https://www.amazon.com.br/',
        campoTag: 'amazonTag',
        parametroTag: 'tag',
        campoModelo: 'amazonAffiliateTemplate',
        campoParametros: 'amazonAffiliateParams'
    },
    mercadolivre: {
        rotulo: '🛒 Mercado Livre',
        nome: 'Mercado Livre',
        termos: ['mercado livre', 'mercadolivre', 'mercadolibre', 'meli'],
        dominios: ['mercadolivre.com', 'mercadolibre.com', 'meli.la', 'merca.li'],
        dominiosAfiliados: ['/sec/', 'meli.la', 'merca.li'],
        home: 'https://www.mercadolivre.com.br/',
        campoLink: 'mlAffiliateLink',
        campoModelo: 'mlAffiliateTemplate',
        campoParametros: 'mlAffiliateParams'
    },
    shopee: {
        rotulo: '🧡 Shopee',
        nome: 'Shopee',
        termos: ['shopee'],
        // `shope.ee` é o encurtador real da Shopee; o `shp.ee` que estava aqui
        // não casa com ele (`'shope.ee'.includes('shp.ee')` é falso), então
        // todo link curto passava sem ser reconhecido como Shopee.
        dominios: ['shopee.', 'shope.ee', 'shp.ee'],
        dominiosAfiliados: ['s.shopee.com.br', 'shope.ee', 'shp.ee'],
        home: 'https://shopee.com.br/',
        campoModelo: 'shopeeAffiliateTemplate',
        campoParametros: 'shopeeAffiliateParams',
        credenciais: [
            { nome: 'shopeeAppKey', rotulo: 'App ID (Open API)', placeholder: 'Ex: 15364800123' },
            { nome: 'shopeeAppSecret', rotulo: 'App Secret (Open API)', placeholder: 'Chave secreta da Open API', segredo: true }
        ],
        aviso: 'Com App ID e Secret o portal usa a API oficial de afiliados: ela responde de qualquer rede e já devolve o link afiliado pronto, então o modelo abaixo fica desnecessário para a Shopee. As chaves saem em affiliate.shopee.com.br > Open API.'
    },
    aliexpress: {
        rotulo: '🌐 AliExpress',
        nome: 'AliExpress',
        termos: ['aliexpress', 'ali express'],
        dominios: ['aliexpress.com', 'ali.ski'],
        dominiosAfiliados: ['s.click.aliexpress.com', 'ali.ski'],
        home: 'https://pt.aliexpress.com/',
        campoModelo: 'aliAffiliateTemplate',
        campoParametros: 'aliAffiliateParams'
        // Sem `credenciais`: os campos aliApiKey/aliApiSecret/aliTrackingId
        // existiam na tela e acendiam o selo "configurado", mas nenhuma linha
        // do projeto os lia — era uma promessa de recurso que não existe. As
        // colunas continuam no banco (ninguém perde o que digitou), só saíram
        // da UI. O caminho que de fato funciona para o AliExpress é o deeplink
        // s.click com {url}, no campo de modelo.
    },
    kabum: {
        rotulo: '💙 KaBuM!',
        nome: 'KaBuM!',
        termos: ['kabum'],
        dominios: ['kabum.com.br'],
        dominiosAfiliados: ['awin1.com', 'tidd.ly'],
        home: 'https://www.kabum.com.br/',
        campoModelo: 'kabumAffiliateTemplate',
        campoParametros: 'kabumAffiliateParams'
    },
    magalu: {
        rotulo: '🔷 Magalu',
        nome: 'Magalu',
        termos: ['magalu', 'magazine'],
        dominios: ['magazineluiza.com.br', 'magalu.com', 'magazinevoce.com.br'],
        // magazinevoce.com.br É a divulgação afiliada do Magalu: um link desses
        // já está monetizado e não pode ser trocado pelo link fixo.
        dominiosAfiliados: ['magazinevoce.com.br'],
        home: 'https://www.magazineluiza.com.br/',
        campoLink: 'magaluAffiliateLink',
        campoModelo: 'magaluAffiliateTemplate',
        campoParametros: 'magaluAffiliateParams'
    }
};

const ROTULO_PADRAO = '🛍️ Oferta';

/** Rótulos dos campos genéricos, para a UI não precisar adivinhar. */
const TEXTOS_DOS_CAMPOS = {
    campoTag: {
        rotulo: 'Tag de afiliado',
        placeholder: 'Ex: seunome-20',
        ajuda: 'Entra como parâmetro na própria URL do produto.'
    },
    campoLink: {
        rotulo: 'Link de afiliado fixo',
        placeholder: 'Cole o seu link geral de afiliado',
        ajuda: 'Substitui o endereço do produto pelo seu link de divulgação.'
    },
    campoModelo: {
        rotulo: 'Link ou modelo de afiliado',
        placeholder: 'Opcional: deeplink com {url}',
        ajuda: 'Com {url} no meio, o endereço do produto entra no lugar (deeplink). Sem {url}, o link salvo substitui o do produto.'
    },
    campoParametros: {
        rotulo: 'Parâmetros extras (opcional)',
        placeholder: 'Ex: sub_id=grupo-whatsapp',
        ajuda: 'Anexados no fim de qualquer link gerado.'
    }
};

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

/**
 * O link já é de afiliado?
 *
 * Nasceu deste problema: a busca da Shopee pela API oficial devolve o
 * `offerLink`, que já vem monetizado. O /api/post-deal passava esse link por
 * `construirLink` e, se o usuário também tivesse preenchido o modelo da
 * Shopee, o link fixo SUBSTITUÍA o offerLink — a oferta virava a home da loja
 * e a comissão ia para a configuração errada.
 */
function linkJaAfiliado(url, info) {
    const endereco = texto(url).toLowerCase();
    if (!endereco || !info || !Array.isArray(info.dominiosAfiliados)) return false;
    return info.dominiosAfiliados.some(marca => endereco.includes(marca));
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

    // Já monetizado: só os parâmetros extras, nada de reescrever.
    if (linkJaAfiliado(alvo, info)) {
        return comParametros(alvo, campo(settings, info.campoParametros));
    }

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
        const permitidos = info.dominiosTag || info.dominios;
        const noDominio = permitidos.some(dominio => alvo.toLowerCase().includes(dominio));
        if (noDominio) final = comTag(final, info.parametroTag, tag);
    }

    return comParametros(final, campo(settings, info.campoParametros));
}

/**
 * O link que vai sair está monetizado?
 *
 * Nasceu do campo de link direto do portal: colar um endereço e publicar sem
 * saber se ele sai com comissão é descobrir o esquecimento semanas depois, no
 * extrato que não mexeu.
 *
 * A tentação é responder olhando se a loja tem campo preenchido, e essa
 * resposta mente no caso mais comum. O link fixo do Mercado Livre pode estar
 * salvo e ainda assim NÃO entrar: diante da página de um produto específico,
 * `permitirLinkFixo: false` o descarta de propósito, para não trocar a oferta
 * pela home da loja. Dizer "configurado" ali é exatamente o silêncio que esta
 * checagem existe para evitar.
 *
 * Então a resposta é empírica, e sai do próprio `construirLink` em vez de
 * repetir as suas regras: monta o link com a configuração inteira e monta de
 * novo com SÓ os parâmetros extras. Se os dois são iguais, nenhuma regra de
 * monetização entrou — os parâmetros (sub_id, utm_source) rastreiam, não pagam.
 */
function linkSaiAfiliado(plataforma, url, settings = {}, opcoes = {}) {
    const encontrado = identificar(plataforma, url);
    if (!encontrado) return false;

    const { info } = encontrado;
    if (linkJaAfiliado(url, info)) return true;

    const soParametros = { [info.campoParametros]: campo(settings, info.campoParametros) };
    return construirLink(plataforma, url, settings, opcoes) !== construirLink(plataforma, url, soParametros, opcoes);
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
        for (const cred of info.credenciais || []) {
            if (!campos.includes(cred.nome)) campos.push(cred.nome);
        }
    }
    return campos;
}

/** Nomes de campo que guardam segredo — a UI recebe mascarado. */
function camposSecretos() {
    const nomes = [];
    for (const info of Object.values(MARKETPLACES)) {
        for (const cred of info.credenciais || []) {
            if (cred.segredo) nomes.push(cred.nome);
        }
    }
    return nomes;
}

/**
 * Descrição serializável das lojas e dos seus campos.
 * É o que as telas consomem para se desenhar sozinhas.
 */
function esquemaDeAfiliados() {
    return Object.entries(MARKETPLACES).map(([chave, info]) => {
        const campos = [];

        for (const tipo of ['campoTag', 'campoLink', 'campoModelo', 'campoParametros']) {
            const nome = info[tipo];
            if (!nome) continue;
            campos.push({
                nome,
                tipo: 'text',
                segredo: false,
                ...TEXTOS_DOS_CAMPOS[tipo]
            });
        }

        for (const cred of info.credenciais || []) {
            campos.push({
                nome: cred.nome,
                rotulo: cred.rotulo,
                placeholder: cred.placeholder || '',
                ajuda: cred.ajuda || '',
                tipo: cred.segredo ? 'password' : 'text',
                segredo: !!cred.segredo
            });
        }

        return {
            chave,
            nome: info.nome || chave,
            rotulo: info.rotulo,
            home: info.home,
            aviso: info.aviso || '',
            campos
        };
    });
}

module.exports = {
    MARKETPLACES,
    construirLink,
    rotuloDaPlataforma,
    linkDaLoja,
    camposDeAfiliado,
    camposSecretos,
    esquemaDeAfiliados,
    linkJaAfiliado,
    identificar,
    linkSaiAfiliado
};
