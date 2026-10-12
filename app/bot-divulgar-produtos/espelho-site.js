/**
 * Espelho dos grupos no site (Tech Lobo).
 *
 * Toda oferta que o /api/post-deal publica com sucesso (piloto automático ou
 * busca manual do portal) também vai para o catálogo do site, com o MESMO
 * link de afiliado que foi para o grupo. Aviso de cupom (pedido com
 * `cupom_site`, montado pelo cupons.py) vai para a página de cupons do site.
 * O site não publica de novo: ele só mostra o que o grupo já recebeu.
 *
 * Como o site pode estar fora do ar (ou esta máquina sem internet), cada
 * postagem entra numa fila em arquivo (data/espelho-site.json) e sai em lotes.
 * Falhou: tenta de novo mais tarde, com espera crescente. Nada se perde num
 * restart do bot. Postagem com mais de 3 dias na fila é descartada: a oferta
 * provavelmente já acabou.
 *
 * Liga sozinho quando SITE_API_URL e SITE_API_TOKEN existem (o iniciar.ps1
 * preenche a partir de SITE_URL e SITE_TOKEN do config.txt). Para desligar sem
 * apagar o token: ESPELHAR_NO_SITE=0.
 */

const fs = require('fs');
const path = require('path');
const axios = require('axios');

const ROTA = '/api/integracao/postagens';
const ARQUIVO = path.join(__dirname, 'data', 'espelho-site.json');
const LOTE = 20;
const INTERVALO_MS = 30_000;
const ESPERA_MAXIMA_MS = 10 * 60_000;
const VALIDADE_MS = 3 * 24 * 3_600_000;
const LIMITE_DA_FILA = 500;

// Campos que o site entende. O resto do pedido (legenda, grupo, flags) fica aqui.
const CAMPOS = [
    'titulo', 'link', 'preco', 'preco_original', 'cupom', 'cupom_fim', 'imagem_url', 'plataforma',
    'categoria', 'termo_pesquisado', 'frete_gratis', 'vendedor'
];

function lerFila(arquivo) {
    try {
        const dados = JSON.parse(fs.readFileSync(arquivo, 'utf8'));
        return Array.isArray(dados) ? dados : [];
    } catch {
        return [];
    }
}

/** Grava num arquivo temporário e troca: queda de energia no meio não corrompe a fila. */
function gravarFila(arquivo, fila) {
    try {
        fs.mkdirSync(path.dirname(arquivo), { recursive: true });
        const temporario = `${arquivo}.tmp`;
        fs.writeFileSync(temporario, JSON.stringify(fila));
        fs.renameSync(temporario, arquivo);
    } catch (e) {
        console.error(`Espelho do site: não consegui gravar a fila: ${e.message}`);
    }
}

function criarEspelhoDoSite({ base, token, ligado, arquivo = ARQUIVO } = {}) {
    base = String(base ?? process.env.SITE_API_URL ?? '').trim().replace(/\/+$/, '');
    token = String(token ?? process.env.SITE_API_TOKEN ?? '').trim();
    const desligadoNoConfig = String(ligado ?? process.env.ESPELHAR_NO_SITE ?? '1').trim() === '0';

    if (!base || !token || desligadoNoConfig) {
        console.log(
            desligadoNoConfig
                ? 'Espelho do site: desligado (ESPELHAR_NO_SITE=0 no config.txt).'
                : 'Espelho do site: desligado (preencha SITE_URL e SITE_TOKEN no config.txt).'
        );
        return { ligado: false, registrar() {}, enviar: async () => {}, parar() {} };
    }

    const cliente = axios.create({
        baseURL: base,
        timeout: 30_000,
        headers: { Authorization: `Bearer ${token}`, 'User-Agent': 'preco-de-banana/bot' }
    });

    let fila = lerFila(arquivo);
    const salvar = () => gravarFila(arquivo, fila);
    let ocupado = false;
    let falhas = 0;
    let proximaTentativa = 0;
    let ultimoAviso = '';

    function avisar(mensagem) {
        if (mensagem !== ultimoAviso) {
            console.log(`Espelho do site: ${mensagem}`);
            ultimoAviso = mensagem;
        }
    }

    /** Chamado logo depois de uma postagem que deu certo. Não espera o site. */
    function registrar(oferta, { canais = [], linkAfiliado = '' } = {}) {
        let item = {};
        if (oferta.cupom_site && typeof oferta.cupom_site === 'object') {
            // Aviso de cupom: os dados já vêm prontos do cupons.py.
            item = { ...oferta.cupom_site, _tipo: 'cupom' };
            if (!item.codigo || !oferta.link) return;
            item.link = oferta.link;
            item.plataforma = item.plataforma || oferta.plataforma;
        } else {
            for (const campo of CAMPOS) {
                if (oferta[campo] !== undefined && oferta[campo] !== null && oferta[campo] !== '') item[campo] = oferta[campo];
            }
            if (!item.titulo || !item.link) return;
        }
        item.link_afiliado = linkAfiliado || undefined;
        item.canais = canais.map((c) => String(c).toLowerCase());
        item.postado_em = new Date().toISOString();
        item._id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

        fila.push(item);
        // Site fora do ar por muito tempo: fica com as mais recentes.
        if (fila.length > LIMITE_DA_FILA) fila = fila.slice(-LIMITE_DA_FILA);
        salvar();
        // Sai já, sem esperar o próximo ciclo (a não ser que o site esteja falhando).
        setTimeout(() => enviar().catch(() => {}), 2_000);
    }

    async function enviar() {
        if (ocupado || !fila.length || Date.now() < proximaTentativa) return;
        ocupado = true;
        try {
            const limite = Date.now() - VALIDADE_MS;
            const antes = fila.length;
            fila = fila.filter((item) => new Date(item.postado_em).getTime() >= limite);
            if (fila.length !== antes) {
                console.log(`Espelho do site: ${antes - fila.length} postagem(ns) antiga(s) descartada(s) da fila.`);
                salvar();
            }

            while (fila.length) {
                const lote = fila.slice(0, LOTE);
                // Por id, e não por posição: uma postagem nova pode entrar na fila durante o envio.
                const tirarDaFila = () => {
                    const enviados = new Set(lote.map((item) => item._id));
                    fila = fila.filter((item) => !enviados.has(item._id));
                    salvar();
                };
                let resposta;
                try {
                    const limpar = ({ _id, _tipo, ...item }) => item;
                    const itens = lote.filter((item) => item._tipo !== 'cupom').map(limpar);
                    const cupons = lote.filter((item) => item._tipo === 'cupom').map(limpar);
                    resposta = await cliente.post(ROTA, { origem: 'bot', itens, cupons });
                } catch (e) {
                    const status = e.response && e.response.status;
                    // 400: o lote em si é inválido; repetir não adianta.
                    if (status === 400 || status === 413) {
                        const motivo = (e.response.data && e.response.data.erro) || `HTTP ${status}`;
                        console.error(`Espelho do site: lote recusado (${motivo}), descartado.`);
                        tirarDaFila();
                        continue;
                    }
                    throw e;
                }

                tirarDaFila();
                falhas = 0;
                proximaTentativa = 0;

                const d = resposta.data || {};
                const partes = [];
                if (d.criados) partes.push(`${d.criados} nova(s) no catálogo`);
                if (d.atualizados) partes.push(`${d.atualizados} atualizada(s)`);
                if (d.pendentes && d.pendentes.length) partes.push(`${d.pendentes.length} esperando revisão no painel`);
                if (d.linksRecusados) partes.push(`${d.linksRecusados} link(s) de afiliado recusado(s)`);
                if (d.invalidas) partes.push(`${d.invalidas} inválida(s)`);
                const c = d.cupons || {};
                if (c.criados) partes.push(`${c.criados} cupom(ns) novo(s) no site`);
                if (c.atualizados) partes.push(`${c.atualizados} cupom(ns) atualizado(s)`);
                if (c.ignorados && c.ignorados.length) {
                    partes.push(`cupom ignorado: ${c.ignorados.map((i) => i.motivo).slice(0, 2).join('; ')}`);
                }
                ultimoAviso = '';
                console.log(`Espelho do site: ${lote.length} postagem(ns) enviada(s)` + (partes.length ? ` (${partes.join(', ')}).` : '.'));
            }
        } catch (e) {
            falhas += 1;
            const espera = Math.min(ESPERA_MAXIMA_MS, INTERVALO_MS * 2 ** Math.min(falhas - 1, 5));
            proximaTentativa = Date.now() + espera;
            const status = e.response && e.response.status;
            const motivo = (e.response && e.response.data && e.response.data.erro) || e.message;
            avisar(
                (status === 401 ? 'token recusado pelo site (confira SITE_TOKEN).' : `site indisponível (${motivo}).`) +
                ` ${fila.length} postagem(ns) na fila; nova tentativa em ${Math.round(espera / 1000)} s.`
            );
        } finally {
            ocupado = false;
        }
    }

    console.log(`Espelho do site: ligado (${base}).` + (fila.length ? ` ${fila.length} postagem(ns) pendente(s) na fila.` : ''));
    const primeiro = setTimeout(() => enviar().catch(() => {}), 10_000);
    const timer = setInterval(() => enviar().catch(() => {}), INTERVALO_MS);

    return {
        ligado: true,
        registrar,
        enviar,
        pendentes: () => fila.length,
        parar() {
            clearTimeout(primeiro);
            clearInterval(timer);
        }
    };
}

module.exports = { criarEspelhoDoSite };
