#!/usr/bin/env python3
"""
Script de inspección del DOM del menú en el portal SRI autenticado.
Ejecuta y muestra la estructura real del menú para calibrar selectores.
"""
import os, sys
os.environ.setdefault("PYTHONUTF8", "1")
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import asyncio
from sri_scraper.browser import browser_context
from sri_scraper.config import SRIConfig
from sri_scraper.login import login
from sri_scraper.browser import human_delay


async def main():
    config = SRIConfig()
    async with browser_context(config) as ctx:
        print("Haciendo login...")
        page = await login(ctx, config)
        print(f"Login OK. URL: {page.url}")

        # Esperar carga del portal
        print("Esperando carga del portal...")
        await human_delay(3000, 4000)

        # Tomar screenshot
        await page.screenshot(path="logs/inspect_menu.png")
        print("Screenshot guardado: logs/inspect_menu.png")

        # Inspeccionar la estructura del DOM
        result = await page.evaluate("""
        () => {
            const info = {};

            // 1. Todos los elementos visibles con texto de menu
            const allLinks = Array.from(document.querySelectorAll('a, button, [role="menuitem"], [role="button"]'));
            info.all_links = allLinks
                .filter(el => {
                    const text = el.textContent.trim();
                    return text.length > 2 && text.length < 60 && el.offsetParent !== null;
                })
                .map(el => ({
                    tag: el.tagName,
                    text: el.textContent.trim().substring(0, 60),
                    class: el.className.substring(0, 80),
                    href: el.href || '',
                    id: el.id
                }))
                .slice(0, 50);

            // 2. Estructura PrimeNG panelMenu
            const panelHeaders = Array.from(document.querySelectorAll('.ui-panelmenu-header-link, .ui-panelmenu-header a'));
            info.panel_headers = panelHeaders.map(el => ({
                text: el.textContent.trim(),
                class: el.className,
                href: el.href || '',
                visible: el.offsetParent !== null
            }));

            // 3. Sub-items PrimeNG
            const menuItems = Array.from(document.querySelectorAll('.ui-menuitem-link, .ui-menuitem a'));
            info.menu_items = menuItems.map(el => ({
                text: el.textContent.trim(),
                class: el.className,
                href: el.href || '',
                visible: el.offsetParent !== null
            }));

            // 4. Cualquier elemento nav o sidebar
            const navElements = Array.from(document.querySelectorAll('nav, aside, [class*="sidebar"], [class*="menu"], [class*="nav"]'));
            info.nav_containers = navElements.map(el => ({
                tag: el.tagName,
                class: el.className.substring(0, 60),
                id: el.id,
                child_count: el.children.length
            })).slice(0, 20);

            // 5. Texto completo visible de la pagina (primeros 2000 chars)
            info.page_text_snippet = document.body.innerText.substring(0, 2000);

            // 6. Todos los elementos con 'FACTUR' o 'Comprobante' en texto
            const targetEls = Array.from(document.querySelectorAll('*')).filter(el => {
                const text = el.textContent;
                return (text.includes('FACTUR') || text.includes('Comprobante') || text.includes('recibido'))
                    && el.children.length < 3;
            });
            info.target_elements = targetEls.map(el => ({
                tag: el.tagName,
                text: el.textContent.trim().substring(0, 80),
                class: el.className.substring(0, 60),
                id: el.id,
                href: el.href || '',
                visible: el.offsetParent !== null
            })).slice(0, 30);

            return info;
        }
        """)

        print("\n=== PANEL HEADERS (PrimeNG) ===")
        for h in result.get('panel_headers', []):
            print(f"  [{h['visible']}] {repr(h['text'])} | class={h['class'][:40]} | href={h['href'][:60]}")

        print("\n=== MENU ITEMS (PrimeNG sub-items) ===")
        for item in result.get('menu_items', []):
            print(f"  [{item['visible']}] {repr(item['text'])} | href={item['href'][:80]}")

        print("\n=== TARGET ELEMENTS (FACTUR/Comprobante) ===")
        for el in result.get('target_elements', []):
            print(f"  [{el['visible']}] <{el['tag']}> {repr(el['text'])} | class={el['class'][:40]} | href={el['href'][:60]}")

        print("\n=== ALL VISIBLE LINKS ===")
        for link in result.get('all_links', []):
            print(f"  <{link['tag']}> {repr(link['text'])} | class={link['class'][:40]} | href={link['href'][:60]}")

        print("\n=== NAV CONTAINERS ===")
        for nav in result.get('nav_containers', []):
            print(f"  <{nav['tag']}> id={nav['id']} class={nav['class'][:50]} children={nav['child_count']}")

        print("\n=== PAGE TEXT SNIPPET ===")
        print(result.get('page_text_snippet', '')[:1000])


if __name__ == "__main__":
    asyncio.run(main())
