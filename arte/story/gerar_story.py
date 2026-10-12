"""
Gera o story do Tech Ofertas (PNG 1080x1920) a partir de um JSON.

    python gerar_story.py dados.json saida.png [--previa]

dados.json: {"titulo": "...", "preco": 157.3, "preco_de": 437.98, "loja": "AliExpress",
             "imagem": "caminho/da/foto.jpg", "selo": "ACHADO DO DIA", "pagamento": "no Pix",
             "cupom": "TECH10", "cupom_desconto": "10% OFF", "preco_com_cupom": 141.57,
             "data": "12/10 · 16h"}
--previa mostra onde colar o sticker de link.
"""
import base64, json, mimetypes, os, sys
from playwright.sync_api import sync_playwright

PASTA = os.path.dirname(os.path.abspath(__file__))
CHROME = os.getenv("STORY_CHROME") or None


def data_uri(caminho):
    if not caminho or caminho.startswith(("http://", "https://", "data:")):
        return caminho
    tipo = mimetypes.guess_type(caminho)[0] or "image/png"
    with open(caminho, "rb") as arq:
        return f"data:{tipo};base64,{base64.b64encode(arq.read()).decode()}"


def gerar(dados, saida, previa=False):
    dados = dict(dados, imagem=data_uri(dados.get("imagem")), previa=previa)
    with sync_playwright() as p:
        try:
            nav = p.chromium.launch(executable_path=CHROME) if CHROME else p.chromium.launch()
        except Exception:
            nav = p.firefox.launch()   # o portal já tem o Firefox do Playwright instalado
        pagina = nav.new_page(viewport={"width": 1080, "height": 1920}, device_scale_factor=1)
        pagina.add_init_script("window.DADOS = " + json.dumps(dados, ensure_ascii=False) + ";")
        pagina.goto("file://" + os.path.join(PASTA, "story.html").replace("\\", "/"))
        pagina.wait_for_function("window.__pronto === true", timeout=20000)
        pagina.evaluate("document.fonts.ready")
        pagina.wait_for_timeout(150)
        pagina.screenshot(path=saida, clip={"x": 0, "y": 0, "width": 1080, "height": 1920})
        nav.close()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    with open(args[0], encoding="utf-8") as arq:
        gerar(json.load(arq), args[1], previa="--previa" in sys.argv)
    print("ok:", args[1])
