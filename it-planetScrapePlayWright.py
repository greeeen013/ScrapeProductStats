import asyncio
import csv
import io
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl, urljoin

from playwright.async_api import async_playwright, Page

# UTF-8 výstup – oprava pro Windows terminál (cp1252 neumí česky)
if hasattr(sys.stdout, 'buffer') and sys.stdout.encoding.lower().replace('-', '') not in ('utf8', 'utf8sig'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# === KONFIGURACE ===
BASE_URL = "https://it-planet.com"
START_URL = "https://it-planet.com/en"
SCRIPT_DIR = Path(__file__).resolve().parent
PROGRESS_FILE = SCRIPT_DIR / "it-planet_progress_v6.json"
HTML_DUMP_DIR = SCRIPT_DIR / "html_dumps"

STEALTH_JS = """
() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
    window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
    const orig = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) =>
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : orig(parameters);
}
"""


# === SPRÁVA PROGRESSU ===
def save_progress(section, page, done_urls=None):
    try:
        data = {"section": section, "page": page}
        if done_urls is not None:
            data["done_urls"] = list(done_urls)
        with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        dbg(f"Chyba při ukládání progressu: {e}")


def load_progress():
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding='utf-8'))
        except Exception:
            return None
    return None


def clear_progress():
    try:
        if PROGRESS_FILE.exists():
            PROGRESS_FILE.unlink()
    except Exception:
        pass


# === POMOCNÉ FUNKCE ===
def dbg(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


async def dump_page_html(page, label: str):
    """Uloží HTML stránky do souboru pro debug."""
    try:
        HTML_DUMP_DIR.mkdir(exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        safe_label = re.sub(r'[^a-zA-Z0-9_-]', '_', label)[:60]
        path = HTML_DUMP_DIR / f"{ts}_{safe_label}.html"
        html = await page.content()
        path.write_text(html, encoding="utf-8")
        dbg(f"HTML dump uložen: {path}")
    except Exception as e:
        dbg(f"Nepodařilo se uložit HTML dump: {e}")


async def check_cloudflare(page) -> bool:
    """Vrátí True pokud je stránka blokována Cloudflare challenge."""
    try:
        title = await page.title()
        html_snippet = await page.evaluate("() => document.body ? document.body.innerHTML.slice(0, 4000) : ''")
        signals = [
            "just a moment" in title.lower(),
            "cf-browser-verification" in html_snippet,
            "checking your browser" in html_snippet.lower(),
            "challenge-platform" in html_snippet,
            "ray id" in html_snippet.lower() and "cloudflare" in html_snippet.lower(),
            "enable javascript and cookies" in html_snippet.lower(),
        ]
        if any(signals):
            dbg(f"CLOUDFLARE DETEKOVÁN: title={title!r}, url={page.url}")
            return True
    except Exception:
        pass
    return False


def clean_text(text):
    """Vyčistí text pro CSV."""
    if not text:
        return "N/A"
    text = str(text).replace(';', ',').replace('"', "'")
    text = re.sub(r'[\r\n\t]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip() or "N/A"


def parse_price(text):
    if not text or "request" in text.lower() or "anfrage" in text.lower():
        return "On Request"
    cleaned = text.replace('€', '').replace('*', '').strip()
    # Shopware placeholder pro "On Request" cenu
    if cleaned == "9999999" or cleaned.startswith("9999999"):
        return "On Request"
    return cleaned


# === UKLÁDÁNÍ DO CSV ===
class CsvWriter:
    def __init__(self, filepath):
        self.filepath = Path(filepath)
        if not self.filepath.exists():
            with open(self.filepath, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f, delimiter=';', quoting=csv.QUOTE_ALL)
                writer.writerow([
                    'Product Name', 'Condition', 'Price', 'Delivery Time',
                    'Supplier Number', 'Product ID (SKU)', 'Images',
                    'Description', 'Category Path', 'Product URL'
                ])

    def write(self, rows):
        if not rows: return
        with open(self.filepath, 'a', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f, delimiter=';', quoting=csv.QUOTE_ALL)
            for row in rows:
                cleaned_row = [str(item) if item is not None else "N/A" for item in row]
                writer.writerow(cleaned_row)


# === VYLEPŠENÁ EXTRAKCE DAT ===

async def extract_current_variant_data(page: Page, base_data: dict):
    """Vytáhne data z aktuálně viditelné stránky."""
    data = base_data.copy()

    # 1. STAV (Condition)
    try:
        checked_input = page.locator('.configurator--form input[type="radio"]:checked')
        if await checked_input.count() > 0:
            cond = await checked_input.get_attribute("title")
            if not cond:
                cid = await checked_input.get_attribute("id")
                cond = await page.locator(f'label[for="{cid}"]').inner_text()
            data['condition'] = clean_text(cond)
        else:
            data['condition'] = "Standard"
    except:
        data['condition'] = "N/A"

    # 2. CENA
    try:
        price_el = page.locator('.product--price .price--content')
        if await price_el.count() > 0:
            raw_text = await price_el.first.inner_text()
            data['price'] = parse_price(raw_text)
        else:
            meta = page.locator('meta[itemprop="price"]')
            if await meta.count() > 0:
                meta_val = await meta.get_attribute("content") or ""
                data['price'] = parse_price(meta_val)
            else:
                data['price'] = "On Request"
    except:
        data['price'] = "N/A"

    # 3. DODACÍ LHŮTA
    try:
        del_el = page.locator('.delivery--text')
        if await del_el.count() > 0:
            data['delivery_time'] = clean_text(await del_el.inner_text())
        else:
            data['delivery_time'] = "N/A"
    except:
        data['delivery_time'] = "N/A"

    # 4. SKU (Order number)
    try:
        sku_li = page.locator('.entry--sku .entry--content')
        if await sku_li.count() > 0:
            data['sku'] = clean_text(await sku_li.inner_text())
        else:
            sku_el = page.locator('[itemprop="sku"]')
            if await sku_el.count() > 0:
                sku = await sku_el.get_attribute("content")
                if not sku: sku = await sku_el.inner_text()
                data['sku'] = clean_text(sku)
            else:
                data['sku'] = "N/A"
    except:
        data['sku'] = "N/A"

    # 5. OBRÁZKY (OPRAVENO: Cílíme na data-img-original)
    try:
        srcs = []
        # Hledáme elementy, které obsahují data o obrázcích
        # Shopware používá třídu .image--element na wrapperu
        elements = await page.locator('.image--element').all()

        for el in elements:
            # Zkusíme získat URL z data atributu (nejspolehlivější)
            url = await el.get_attribute('data-img-original')
            if not url:
                url = await el.get_attribute('data-img-large')

            # Fallback: pokud data atribut chybí, zkusíme najít img tag uvnitř
            if not url:
                img_tag = el.locator('img').first
                if await img_tag.count() > 0:
                    srcset = await img_tag.get_attribute('srcset')
                    if srcset:
                        url = srcset.split(',')[-1].strip().split(' ')[0]
                    else:
                        url = await img_tag.get_attribute('src')

            if url:
                # Relativní URL doplníme na absolutní
                if url.startswith('/'):
                    url = BASE_URL + url
                if url not in srcs:
                    srcs.append(url)

        data['images'] = ' | '.join(srcs) if srcs else "N/A"
    except Exception as e:
        # dbg(f"Chyba obrázků: {e}")
        data['images'] = "N/A"

    # 6. Supplier Number
    try:
        sup_el = page.locator('.entry--suppliernumber .entry--content')
        if await sup_el.count() > 0:
            data['supplier_number'] = clean_text(await sup_el.inner_text())
        else:
            data['supplier_number'] = "N/A"
    except:
        data['supplier_number'] = "N/A"

    return data


async def scrape_product(context, url, semaphore):
    async with semaphore:
        page = await context.new_page()
        all_rows = []

        try:
            await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)

            if await check_cloudflare(page):
                await dump_page_html(page, f"cloudflare_{url.split('/')[-1].split('?')[0]}")
                raise RuntimeError("Cloudflare challenge – stránka zablokována")

            # SPOLEČNÁ DATA
            try:
                h1 = page.locator('h1.product--title')
                name = clean_text(await h1.inner_text()) if await h1.count() > 0 else 'N/A'

                desc_parts = []
                desc_txt = page.locator('.product--description').first
                if await desc_txt.count() > 0:
                    desc_parts.append(clean_text(await desc_txt.inner_text()))

                rows = await page.locator('.product--description .table.d-table tr').all()
                specs = []
                for r in rows:
                    cols = await r.locator('td').all()
                    if len(cols) >= 2:
                        k = await cols[0].inner_text()
                        v = await cols[1].inner_text()
                        specs.append(f"{clean_text(k)}: {clean_text(v)}")
                if specs:
                    desc_parts.append("SPECS: " + " | ".join(specs))

                full_desc = " ; ".join(desc_parts)

                cats = []
                crumbs = await page.locator('.breadcrumb--entry span').all()
                for c in crumbs:
                    t = await c.inner_text()
                    if t: cats.append(clean_text(t))
                cat_path = " > ".join(cats) if cats else "N/A"

            except Exception as e:
                dbg(f"Chyba základu: {e}")
                name, full_desc, cat_path = "N/A", "N/A", "N/A"

            base_template = {
                'product_name': name,
                'category_path': cat_path,
                'description': full_desc,
                'url': url
            }

            # VARIANTY
            # Hledáme radio inputy
            variant_inputs = page.locator('.configurator--form input[type="radio"]')
            count = await variant_inputs.count()

            if count == 0:
                row = await extract_current_variant_data(page, base_template)
                all_rows.append(row)
            else:
                # Seznam ID pro klikání
                input_ids = []
                for i in range(count):
                    i_id = await variant_inputs.nth(i).get_attribute("id")
                    if i_id: input_ids.append(i_id)

                for i_id in input_ids:
                    label = page.locator(f'label[for="{i_id}"]')
                    if await label.count() > 0:
                        try:
                            # 1. Klik
                            await label.click(force=True)
                            # 2. Čekání na AJAX
                            await page.wait_for_timeout(2000)
                            # 3. Extrakce
                            row = await extract_current_variant_data(page, base_template)
                            all_rows.append(row)
                        except:
                            pass

            # Zápis
            final_rows = []
            for d in all_rows:
                final_rows.append([
                    d.get('product_name', 'N/A'),
                    d.get('condition', 'N/A'),
                    d.get('price', 'N/A'),
                    d.get('delivery_time', 'N/A'),
                    d.get('supplier_number', 'N/A'),
                    d.get('sku', 'N/A'),
                    d.get('images', 'N/A'),
                    d.get('description', 'N/A'),
                    d.get('category_path', 'N/A'),
                    d.get('url', 'N/A')
                ])

            return final_rows, url

        except Exception as e:
            dbg(f"CHYBA při zpracování {url}: {e}")
            try:
                if not page.is_closed():
                    is_cf = await check_cloudflare(page)
                    label = f"{'cloudflare' if is_cf else 'error'}_{url.split('/')[-1].split('?')[0]}"
                    await dump_page_html(page, label)
            except Exception:
                pass
            return [], url
        finally:
            try:
                await page.close()
            except Exception:
                pass


# === PROCHÁZENÍ KATEGORIÍ ===

FALLBACK_SECTIONS = {
    "Networking":    f"{BASE_URL}/en/c/networking.html",
    "Storage":       f"{BASE_URL}/en/c/storage.html",
    "Server":        f"{BASE_URL}/en/c/server.html",
    "Power Supply":  f"{BASE_URL}/en/c/power-supply.html",
}

async def get_sections(context):
    page = await context.new_page()
    dbg("Načítám menu...")
    sections = {}

    try:
        try:
            await page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            dbg(f"Homepage load warning (pokračuji): {e}")
        await page.wait_for_timeout(2000)

        if await check_cloudflare(page):
            await dump_page_html(page, "cloudflare_homepage")
            dbg("FATAL: Homepage blokována Cloudflare – používám záložní sekce")
            return FALLBACK_SECTIONS

        ignored = {"blog", "service", "inquiry", "home", "brands", "manufacturer",
                   "about", "contact", "career", "imprint", "privacy", "terms",
                   "shipping", "warranty", "returns", "disposal", "safety",
                   "declaration", "partner", "right-of", "data-protection",
                   "general-terms", "inquiry-form"}

        menu_items = await page.locator('.navigation--list .navigation--entry .navigation--link').all()

        for item in menu_items:
            title = await item.get_attribute("title")
            href = await item.get_attribute("href")
            if not (title and href):
                continue
            clean_title = title.strip()
            if "SupplierModified" in href:
                continue
            if any(ign in clean_title.lower() for ign in ignored):
                continue
            # Only accept product category URLs (/en/c/ pattern)
            if "/en/c/" not in href and href.rstrip("/") != START_URL.rstrip("/"):
                continue
            if "/en/c/" in href:
                full_url = urljoin(BASE_URL, href)
                sections[clean_title] = full_url

        if not sections:
            dbg("Nepodařilo se načíst sekce z menu, používám záložní seznam")
            sections = dict(FALLBACK_SECTIONS)

    except Exception as e:
        dbg(f"Chyba načítání sekcí: {e}")
        try:
            await dump_page_html(page, "error_sections")
        except Exception:
            pass
        sections = dict(FALLBACK_SECTIONS)
    finally:
        try:
            await page.close()
        except Exception:
            pass

    return sections


async def get_listing_urls(page: Page, section_url, page_num):
    target_url = section_url
    if page_num > 1:
        parsed = urlparse(section_url)
        q = dict(parse_qsl(parsed.query))
        q["p"] = str(page_num)
        new_q = urlencode(q, doseq=True)
        target_url = urlunparse(parsed._replace(query=new_q))

    dbg(f"  > Listing str {page_num}: {target_url}")

    try:
        try:
            await page.goto(target_url, timeout=60000, wait_until="domcontentloaded")
        except Exception as e:
            dbg(f"Listing load warning (pokračuji): {e}")

        if await check_cloudflare(page):
            await dump_page_html(page, f"cloudflare_listing_p{page_num}")
            dbg("FATAL: Listing stránka blokována Cloudflare")
            return []

        title = await page.title()
        dbg(f"  Titulek: {title}")

        # Počkej na produkty – JS je renderuje asynchronně po DOMContentLoaded
        try:
            await page.wait_for_selector(".product--box", timeout=15000)
        except Exception:
            pass  # timeout = žádné produkty (konec sekce)

        box_count = await page.locator(".product--box").count()
        dbg(f"  product--box count: {box_count}")

        # Prázdná stránka – konec sekce (ignorujeme skryté alerty)
        if await page.locator(".alert.is--info:not(.is--hidden)").count() > 0:
            return []
        if box_count == 0:
            dbg("  Zadne product--box elementy, konec sekce")
            await dump_page_html(page, f"empty_listing_p{page_num}")
            return []

        links = []
        buttons = await page.locator('.product--box .product--detail-btn a').all()
        for btn in buttons:
            href = await btn.get_attribute('href')
            if href:
                links.append(href)

        if not links:
            titles = await page.locator('.product--box .product--title').all()
            for t in titles:
                href = await t.get_attribute('href')
                if href:
                    links.append(href)

        dbg(f"  Nalezenych produktu: {len(set(links))}")
        return list(set(links))

    except Exception as e:
        dbg(f"Chyba listingu: {e}")
        try:
            await dump_page_html(page, f"error_listing_p{page_num}")
        except Exception:
            pass
        return []


async def run_test():
    print("=== IT-Planet Test ===")
    try:
        PROXY_URL = "socks5://127.0.0.1:40000"
        proxy_ok = await test_proxy(PROXY_URL)
        proxy_cfg = {"server": PROXY_URL} if proxy_ok else None
        if not proxy_ok:
            print("[test] Proxy nedostupná – připojuji přímo")

        async with async_playwright() as p:
            launch_kw = dict(
                headless=True,
                args=["--disable-gpu", "--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            if proxy_cfg:
                launch_kw["proxy"] = proxy_cfg
            browser = await p.chromium.launch(**launch_kw)
            context = await browser.new_context(
                viewport={"width": 1600, "height": 1000},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            )
            await context.add_init_script(STEALTH_JS)

            sections = await get_sections(context)
            if not sections:
                print("TEST ERROR: Nepodařilo se načíst sekce")
                await browser.close()
                sys.exit(1)

            sec_name = list(sections.keys())[0]
            sec_url = list(sections.values())[0]

            page = await context.new_page()
            urls = await get_listing_urls(page, sec_url, 1)
            await page.close()
            await browser.close()

            print(f"TEST OK: {len(sections)} sekcí načteno, '{sec_name}': {len(urls)} produktů na straně 1")
            sys.exit(0)
    except Exception as e:
        print(f"TEST ERROR: {e}")
        sys.exit(1)


async def test_proxy(proxy_url: str) -> bool:
    """Otestuje dostupnost SOCKS5 proxy jednoduchým TCP spojením."""
    import socket
    try:
        host, port = proxy_url.replace("socks5://", "").split(":")
        s = socket.create_connection((host, int(port)), timeout=3)
        s.close()
        return True
    except Exception:
        return False


# === MAIN ===
async def main():
    if '--test' in sys.argv:
        await run_test()
        return

    print("=== IT-Planet Scraper ===")

    out_name = "it-planet_data.csv"

    w_input = input("Počet paralelních oken (doporučeno 3-5) [3]: ").strip()
    max_concurrent = int(w_input) if w_input.isdigit() else 3

    headless_input = input("Headless režim? (ano/ne, enter=ano): ").strip().lower()
    headless = headless_input != "ne"

    PROXY_URL = "socks5://127.0.0.1:40000"
    proxy_ok = await test_proxy(PROXY_URL)
    if proxy_ok:
        print(f"Proxy {PROXY_URL} dostupná – používám.")
        proxy_cfg = {"server": PROXY_URL}
    else:
        print(f"Proxy {PROXY_URL} nedostupna - pripojuji primo.")
        proxy_cfg = None

    async with async_playwright() as p:
        launch_kwargs = dict(
            headless=headless,
            args=[
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        if proxy_cfg:
            launch_kwargs["proxy"] = proxy_cfg

        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            viewport={"width": 1600, "height": 1000},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            },
        )
        await context.add_init_script(STEALTH_JS)

        try:
            sections = await get_sections(context)
        except Exception as e:
            print(f"Chyba menu: {e}")
            await browser.close()
            return

        names = list(sections.keys())
        print("\nSekce k dispozici:")
        for i, n in enumerate(names, 1):
            print(f"  {i}. {n}")

        choice = input("\nVyber sekci (číslo, název, nebo 'vse'): ").strip()

        selected = []
        if choice.lower() in ('vse', 'all'):
            selected = names
        elif choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(names):
                selected = [names[idx]]
        else:
            for n in names:
                if choice.lower() in n.lower():
                    selected.append(n)

        if not selected:
            print("Nic nevybráno.")
            await browser.close()
            return

        # Načtení progressu
        start_sec_name = None
        start_page = 1
        progress = load_progress()
        if progress:
            print(f"\nNalezen uložený postup: sekce '{progress['section']}', strana {progress['page']}")
            ans = input("Pokračovat od posledního místa? (ano/ne): ").strip().lower()
            if ans == 'ano':
                start_sec_name = progress['section']
                start_page = progress['page']
            else:
                clear_progress()
                progress = None

        resume_done_urls = set(progress.get('done_urls', [])) if progress and start_sec_name else set()
        is_on_resume_page = start_sec_name is not None

        writer = CsvWriter(out_name)
        semaphore = asyncio.Semaphore(max_concurrent)

        total_cnt = 0
        skip_to_sec = start_sec_name is not None

        for sec_name in selected:
            if skip_to_sec:
                if sec_name != start_sec_name:
                    print(f"  (přeskakuji sekci: {sec_name})")
                    continue
                else:
                    skip_to_sec = False

            sec_url = sections[sec_name]
            print(f"\n>>> Zpracovávám: {sec_name}")

            page_obj = await context.new_page()
            curr_page = start_page if sec_name == start_sec_name else 1
            start_page = 1

            while True:
                urls = await get_listing_urls(page_obj, sec_url, curr_page)
                if not urls:
                    print(f"  > Konec {sec_name} (str {curr_page} bez produktů)")
                    break

                if is_on_resume_page:
                    done_urls_page = resume_done_urls
                    is_on_resume_page = False
                else:
                    done_urls_page = set()

                filtered = [u for u in urls if u not in done_urls_page]
                skipped = len(urls) - len(filtered)
                if skipped:
                    print(f"  > Strana {curr_page}: {len(urls)} produktů ({skipped} přeskočeno). Zpracovávám {len(filtered)}...")
                else:
                    print(f"  > Strana {curr_page}: {len(urls)} produktů. Zpracovávám...")

                if filtered:
                    tasks = []
                    for u in filtered:
                        tasks.append(asyncio.create_task(scrape_product(context, u, semaphore)))

                    for res in asyncio.as_completed(tasks):
                        rows, url = await res
                        if rows:
                            writer.write(rows)
                            total_cnt += 1
                            done_urls_page.add(url)
                            save_progress(sec_name, curr_page, done_urls_page)

                save_progress(sec_name, curr_page + 1)
                curr_page += 1

            await page_obj.close()
            clear_progress()

        print(f"\nHOTOVO. Celkem: {total_cnt}")
        clear_progress()
        await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStop.")