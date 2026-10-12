"""
Login do Mercado Livre para o caçador de cupons (rodar uma vez).

A central de cupons do Mercado Livre só abre logada. Este script abre o
Firefox do próprio programa na página de cupons; você entra na conta ali, e a
sessão fica salva em data/profiles/mercadolivre_cupons.json — o mesmo lugar
onde o programa já guarda os cookies das lojas, fora do controle de versão.

Separado de propósito do perfil "mercadolivre" das buscas: as buscas de
produtos continuam anônimas; só a leitura da central de cupons usa a conta.

Uso: LOGIN-MERCADOLIVRE.bat (na raiz do pendrive).
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright  # noqa: E402

import antibot  # noqa: E402
import cupons  # noqa: E402

LIMITE_MINUTOS = 15


def main() -> int:
    caminho = cupons.caminho_sessao_ml()
    os.makedirs(os.path.dirname(caminho), exist_ok=True)

    print()
    print("  Vai abrir uma janela do Firefox na página de cupons do Mercado Livre.")
    print("  Entre na sua conta por ela (e-mail/senha, código por SMS etc.).")
    print("  Quando a lista de cupons aparecer, a janela fecha sozinha.")
    print(f"  Tempo máximo: {LIMITE_MINUTOS} minutos.")
    print()

    salvo = False
    with sync_playwright() as p:
        navegador = p.firefox.launch(headless=False, firefox_user_prefs=antibot.firefox_prefs())
        opcoes = antibot.context_options(navegador)
        if os.path.exists(caminho):
            opcoes["storage_state"] = caminho
        contexto = navegador.new_context(**opcoes)
        antibot.prepare_context(contexto)
        pagina = contexto.new_page()
        pagina.goto(cupons.ML_CENTRAL, wait_until="domcontentloaded", timeout=60000)

        limite = time.time() + LIMITE_MINUTOS * 60
        while time.time() < limite:
            try:
                nomes = {c["name"] for c in contexto.cookies()}
                endereco = pagina.url
            except Exception:
                break  # janela fechada pelo usuário
            if "orguseridp" in nomes and "/login" not in endereco and "/cupons" in endereco:
                time.sleep(4)  # deixa os últimos cookies da sessão chegarem
                contexto.storage_state(path=caminho)
                salvo = True
                break
            if "orguseridp" in nomes and "/cupons" not in endereco and "/login" not in endereco:
                try:
                    pagina.goto(cupons.ML_CENTRAL, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass
            time.sleep(2)

        try:
            contexto.close()
            navegador.close()
        except Exception:
            pass

    if not salvo:
        print("  ✗ Não detectei o login. Rode de novo e entre na conta pela janela que abrir.")
        return 1

    print("  ✓ Sessão salva. Conferindo os cupons...")
    try:
        lidos = cupons.ler_cupons_ml("computing")
        print(f"  ✓ Funcionou: {len(lidos)} cupons gerais em Informática agora.")
        for c in lidos[:5]:
            minimo = cupons.brl(c["minimo"]) if c.get("minimo") else "sem mínimo"
            print(f"     - {c['titulo']} (compra mínima {minimo})")
    except cupons.SessaoMLExpirada as exc:
        print(f"  ✗ {exc}")
        return 1
    except Exception as exc:
        print(f"  ! Sessão salva, mas a conferência falhou ({exc}). O piloto tenta de novo sozinho.")
    print()
    print("  Pronto. O piloto passa a ler os cupons do Mercado Livre no próximo ciclo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
