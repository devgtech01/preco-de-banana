const axios = require('axios');
const cheerio = require('cheerio');
const { construirLink } = require('./afiliados');

const CABECALHOS_NAVEGADOR = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7'
};

function emReais(valor) {
    return `R$ ${Number(valor).toFixed(2).replace('.', ',')}`;
}

async function scrapeAmazon(url, affiliateTag) {
    try {
        // Amazon costuma bloquear scrapers, então usamos um User-Agent real
        const response = await axios.get(url, {
            timeout: 10000,
            headers: {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
                'Accept-Language': 'pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7',
            }
        });
        
        // PEGAR A URL FINAL RESOLVIDA (Isso resolve o problema de links amzn.to curtos que não aceitavam tag)
        const finalUrl = response.request.res.responseUrl || url;
        
        const $ = cheerio.load(response.data);
        
        let title = $('#productTitle').text().trim();
        if (!title) title = $('meta[property="og:title"]').attr('content') || 'Produto Amazon';
        
        let price = $('.a-price .a-offscreen').first().text().trim();
        if (!price) {
            price = $('#priceblock_ourprice').text().trim() || $('#priceblock_dealprice').text().trim() || 'Preço na loja';
        }

        // Tenta buscar o preço original (riscado) específico
        let originalPrice = $('span[data-a-strike="true"] .a-offscreen').first().text().trim();
        
        if (!originalPrice) {
            originalPrice = $('.a-text-price .a-offscreen').first().text().trim() || '';
        }

        // Validação de segurança: O preço "De" nunca pode ser menor que o "Por".
        // Acontece quando a Amazon mostra (R$ 19,00 / unidade) e o scraper se confunde.
        try {
            const numOriginal = parseInt(originalPrice.replace(/\D/g, ''));
            const numCurrent = parseInt(price.replace(/\D/g, ''));
            if (numOriginal > 0 && numCurrent > 0 && numOriginal <= numCurrent) {
                originalPrice = ''; // Descarta pois é o valor unitário
            }
        } catch (e) {}

        let image = $('#landingImage').attr('src') || $('#imgBlkFront').attr('src');
        if (!image) image = $('meta[property="og:image"]').attr('content') || 'https://via.placeholder.com/600x400?text=Imagem+Nao+Encontrada';

        // Melhorar drasticamente a qualidade da imagem da Amazon (remover o redimensionamento/cortes da URL)
        // Exemplo: converte ".../I/foto._AC_SX425_.jpg" para a versão MÁXIMA original ".../I/foto.jpg"
        if (image.includes('images-amazon.com') || image.includes('media-amazon.com')) {
            image = image.replace(/\._[A-Za-z0-9_,-]+_\./g, '.');
        }

        // Anexar a tag de afiliado na URL FINAL (já expandida)
        let affiliateUrl = finalUrl;
        try {
            const parsedUrl = new URL(finalUrl);
            parsedUrl.searchParams.set('tag', affiliateTag || 'suatag-20');
            
            // Remove lixos de rastreamento antigo que vem no amzn.to
            parsedUrl.searchParams.delete('linkCode');
            parsedUrl.searchParams.delete('smid');
            parsedUrl.searchParams.delete('th');
            
            affiliateUrl = parsedUrl.toString();
        } catch (e) {
            affiliateUrl = finalUrl + (finalUrl.includes('?') ? '&' : '?') + 'tag=' + (affiliateTag || 'suatag-20');
        }

        return {
            title,
            price, // Preço atual (Por)
            originalPrice, // Preço original (De)
            image,
            affiliateUrl,
            store: 'Amazon'
        };
    } catch (error) {
        console.error('Erro do scraper da Amazon:', error.message);
        throw new Error('Não foi possível extrair os dados da Amazon. Verifique o link e tente novamente.');
    }
}

async function scrapeML(url) {
    try {
        const response = await axios.get(url, {
            timeout: 10000,
            headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' }
        });
        const finalUrl = response.request.res.responseUrl || url;
        const $ = cheerio.load(response.data);
        
        // Tenta pegar o H1 que é muito mais limpo no ML
        let title = $('h1.ui-pdp-title').text().trim() || $('meta[property="og:title"]').attr('content') || 'Produto Mercado Livre';
        let image = $('meta[property="og:image"]').attr('content') || 'https://via.placeholder.com/600x400';
        
        // Extrai o Preço Antigo (Cruza-se sempre a Tag <s> ou a classe de original-value)
        let originalPrice = $('.ui-pdp-price__original-value .andes-money-amount__fraction').first().text().trim() || $('s .andes-money-amount__fraction').first().text().trim();

        // Pega o Preço Promocional exato: Ignora spans que estejam "riscados" (filhos de <s> ou original-value)
        let price = '';
        $('.andes-money-amount__fraction').each((i, el) => {
            if (!price && !$(el).closest('s').length && !$(el).closest('.ui-pdp-price__original-value').length) {
                price = $(el).text().trim();
            }
        });

        price = price ? `R$ ${price}` : 'Preço Especial no Site';
        originalPrice = originalPrice ? `R$ ${originalPrice}` : '';

        // Prevenção extra
        if (price === originalPrice) originalPrice = '';

        return { title, price, originalPrice, image, affiliateUrl: finalUrl, store: 'Mercado Livre' };
    } catch (e) {
        return { title: 'Mercado Livre', price: 'Ver na loja', originalPrice: '', image: '', affiliateUrl: url, store: 'Mercado Livre' };
    }
}

async function scrapeShopeeAli(url, storeName) {
    try {
        const response = await axios.get(url, { 
            timeout: 10000,
            headers: { 'User-Agent': 'facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)' } 
        });
        const finalUrl = response.request.res.responseUrl || url;
        const $ = cheerio.load(response.data);
        
        let title = $('meta[property="og:title"]').attr('content') || `Oferta ${storeName}`;
        let image = $('meta[property="og:image"]').attr('content') || 'https://via.placeholder.com/600x400';
        
        let price = '[DIGITE O VALOR AQUI]';
        let originalPrice = '[O SEU VALOR ANTIGO]';

        // AliExpress costuma injetar o preço direto no Título do link escondido! (Ex: R$ 15,00 | Fone Bluetooh)
        if (storeName === 'AliExpress') {
            const priceMatch = title.match(/R\$\s?[\d.,]+/);
            if (priceMatch) {
                price = priceMatch[0]; // Extrai só a parte do R$ e os números
                title = title.replace(/R\$\s?[\d.,]+.*?\|?/, '').replace(/\|/g, '').trim(); // Remove o preço do título
            }
        }

        return { title, price, originalPrice, image, affiliateUrl: finalUrl, store: storeName };
    } catch(e) {
        return { title: `Oferta ${storeName}`, price: '[VALOR AQUI]', originalPrice: '[ANTIGO]', image: '', affiliateUrl: url, store: storeName };
    }
}

async function scrapeMagalu(url) {
    try {
        // Bypass na página de "Verificando se você é um robô" (az-request-verify)
        if (url.includes('az-request-verify?url=')) {
            const urlObj = new URL(url);
            url = decodeURIComponent(urlObj.searchParams.get('url'));
        }

        const response = await axios.get(url, { 
            timeout: 10000,
            headers: { 
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                'Accept-Language': 'pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7',
                'Sec-Fetch-Mode': 'navigate'
            } 
        });
        const finalUrl = response.request.res.responseUrl || url;
        const $ = cheerio.load(response.data);
        
        let title = $('title').text().replace(/ - Magazine.*/, '').trim() || $('meta[property="og:title"]').attr('content') || 'Produto Magalu';
        let image = $('meta[property="og:image"]').attr('content') || 'https://via.placeholder.com/600x400';
        
        let price = '';
        let originalPrice = '';

        // Tentar extrair preço dos dados estruturados (JSON-LD) que ficam ocultos no HTML
        $('script[type="application/ld+json"]').each((i, el) => {
            try {
               const json = JSON.parse($(el).html());
               if(json.offers) {
                   price = json.offers.price || json.offers.lowPrice;
               }
            } catch(e) {}
        });

        if (price) {
            price = `R$ ${parseFloat(price).toFixed(2).replace('.', ',')}`;
        } else {
            // Fallback visual
            price = $('[data-testid="price-value"]').first().text().trim() || 'Preço Especial no Site';
        }

        return { title, price, originalPrice, image, affiliateUrl: finalUrl, store: 'Magalu' };
    } catch (e) {
        return { title: 'Produto Magalu', price: 'Ver na loja', originalPrice: '', image: '', affiliateUrl: url, store: 'Magalu' };
    }
}

/**
 * Raspa a pagina da KaBuM!.
 *
 * A KaBuM! e um site Next.js: o preco exato vem no JSON do __NEXT_DATA__, que
 * nao muda de formato a cada deploy como as classes de CSS. O og: fica como
 * fonte do titulo e da imagem, que sao estaveis.
 */
async function scrapeKabum(url) {
    try {
        const response = await axios.get(url, { timeout: 10000, headers: CABECALHOS_NAVEGADOR });
        const finalUrl = response.request.res.responseUrl || url;
        const $ = cheerio.load(response.data);

        let title = ($('meta[property="og:title"]').attr('content') || '')
            .replace(/\s*[-|]\s*KaBuM!?.*$/i, '')
            .trim() || 'Produto KaBuM!';
        let image = $('meta[property="og:image"]').attr('content') || 'https://via.placeholder.com/600x400';

        let price = '';
        let originalPrice = '';

        try {
            const dados = JSON.parse($('#__NEXT_DATA__').html() || '{}');
            const precos = (((dados.props || {}).pageProps || {}).product || {}).prices || {};
            const atual = Number(precos.priceWithDiscount || precos.price);
            const antigo = Number(precos.price || precos.oldPrice);

            if (atual > 0) price = emReais(atual);
            if (antigo > atual) originalPrice = emReais(antigo);
        } catch (e) {
            // Layout mudou: cai para o preco visivel logo abaixo.
        }

        if (!price) {
            price = $('[class*="finalPrice"]').first().text().trim()
                || $('[class*="priceCard"]').first().text().trim()
                || 'Preço Especial no Site';
        }

        return { title, price, originalPrice, image, affiliateUrl: finalUrl, store: 'KaBuM!' };
    } catch (e) {
        return { title: 'Produto KaBuM!', price: 'Ver na loja', originalPrice: '', image: '', affiliateUrl: url, store: 'KaBuM!' };
    }
}

// Sistema Automático de Identificação do Marketplace
async function extrairDados(url, configs) {
    if (url.includes('amazon.') || url.includes('amzn.to')) {
        return scrapeAmazon(url, configs.amazonTag);
    } else if (url.includes('magazineluiza.com.br') || url.includes('magalu.com') || url.includes('magazinevoce.com.br')) {
        return scrapeMagalu(url);
    } else if (url.includes('mercadolivre.com') || url.includes('meli.la') || url.includes('merca.li')) {
        return scrapeML(url);
    } else if (url.includes('kabum.com.br')) {
        return scrapeKabum(url);
    } else if (url.includes('shopee.') || url.includes('shp.ee')) {
        return scrapeShopeeAli(url, 'Shopee');
    } else if (url.includes('aliexpress.com') || url.includes('ali.ski') || url.includes('a.aliexpress.com')) {
        return scrapeShopeeAli(url, 'AliExpress');
    }

    throw new Error('Link não reconhecido! Por enquanto damos suporte a Amazon, Mercado Livre, Magalu, KaBuM!, Shopee e AliExpress.');
}

async function scrapeProduct(url, configs) {
    const dados = await extrairDados(url, configs || {});

    // Regra de afiliado da loja (ver afiliados.js). Aqui o link fixo NÃO
    // substitui o endereço: quem colou o link de um produto específico quer
    // divulgar aquele produto, não a home da loja. Modelos com {url}, tags e
    // parâmetros extras continuam valendo normalmente.
    dados.affiliateUrl = construirLink(dados.store, dados.affiliateUrl, configs || {}, { permitirLinkFixo: false });

    return dados;
}

module.exports = { scrapeProduct };
