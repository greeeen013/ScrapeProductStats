import asyncio
import csv
import json
import re
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl

from playwright.async_api import async_playwright, Page

import openpyxl
from openpyxl import Workbook

# === KONFIGURACE ===
HOMEPAGE = "https://it-market.com/en"
SCRIPT_DIR = Path(__file__).resolve().parent
PROGRESS_FILE = SCRIPT_DIR / "it-marketScrapeLastProduct.json"


# === POMOCNÉ FUNKCE PRO LOGOVÁNÍ ===
def dbg(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


# === SPRÁVA PROGRESSU ===
async def save_progress(section, page, product_idx, url):
    data = {
        "section": section,
        "page": page,
        "product_idx_on_page": product_idx,
        "url": url,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
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


# === PARSING LOGIKA ===
def parse_price_block(text):
    if not text:
        return 'N/A', 'N/A'
    cleaned = text.replace('*', '').strip()
    lines = [l.strip() for l in cleaned.splitlines() if l.strip()]
    price = lines[0] if lines else 'N/A'

    net_price = 'N/A'
    m = re.search(r'(?i)\bnet\s*:\s*(.+)', cleaned)
    if m:
        net_price = m.group(1).strip()
    return price, net_price


async def get_description_and_properties(page: Page):
    """Získá popis a vlastnosti podle poskytnutého HTML."""
    full_text = []

    try:
        desc_pane = page.locator('#description-tab-pane').first

        if await desc_pane.count() > 0:
            is_visible = await desc_pane.is_visible()
            if not is_visible:
                btn = page.locator('button[aria-controls="description-tab-pane"]').first
                if await btn.count() > 0:
                    await btn.click(force=True)
                    await page.wait_for_timeout(500)

        desc_text_el = page.locator('.product-detail-description-text').first
        if await desc_text_el.count() > 0:
            text = await desc_text_el.inner_text()
            if text:
                full_text.append(text.strip())

        rows = await desc_pane.locator('.product-detail-properties-table .properties-row').all()

        properties_list = []
        for row in rows:
            label_el = row.locator('.properties-label')
            val_el = row.locator('.properties-value')

            if await label_el.count() > 0 and await val_el.count() > 0:
                label = await label_el.inner_text()
                val = await val_el.inner_text()

                label = label.strip().rstrip(':')
                val = val.strip()

                if label and val:
                    properties_list.append(f"{label}: {val}")

        if properties_list:
            full_text.append(" | ".join(properties_list))

    except Exception as e:
        dbg(f"Chyba při čtení popisu: {e}")

    return " ; ".join(full_text)


async def extract_variant_data(page: Page, base_data: dict):
    """Extrahuje data z aktuálně vybrané varianty na stránce."""
    data = base_data.copy()

    try:
        checked_radio = page.locator('.product-detail-configurator-option input[type="radio"]:checked')

        if await checked_radio.count() > 0:
            input_id = await checked_radio.get_attribute("id")
            label = page.locator(f'label[for="{input_id}"]')

            condition = await label.get_attribute("title")
            if not condition:
                condition = await label.inner_text()
            data['condition'] = condition.strip() if condition else 'N/A'

            price_block = label.locator('.product-detail-configurator-option-label-prices')
            if await price_block.count() == 0:
                price_block = page.locator('.product-detail-configurator-option-label-prices').first

            price_text = await price_block.inner_text() if await price_block.count() > 0 else ''
            p, np = parse_price_block(price_text)
            data['price'] = p
            data['net_price'] = np

            stock_el = label.locator('.product-detail-configurator-option-inStock')
            del_el = label.locator('.product-detail-configurator-option-withDelivery')

            if await stock_el.count() > 0:
                data['stock_status'] = (await stock_el.inner_text()).strip()
            elif await del_el.count() > 0:
                data['stock_status'] = (await del_el.inner_text()).strip()
            else:
                data['stock_status'] = 'N/A'
        else:
            data.update(
                {'condition': 'Check Description', 'price': 'Check Site', 'net_price': 'N/A', 'stock_status': 'N/A'})

            price_wrapper = page.locator('.product-detail-price-container').first
            if await price_wrapper.count() > 0:
                raw_p = await price_wrapper.inner_text()
                p, np = parse_price_block(raw_p)
                data['price'] = p
                data['net_price'] = np

    except Exception as e:
        dbg(f"Chyba detailů varianty: {e}")

    try:
        qty = page.locator('.product-detail-quantity-available')
        data['quantity_available'] = (await qty.inner_text()).strip() if await qty.count() > 0 else 'N/A'
    except:
        data['quantity_available'] = 'N/A'

    try:
        data['delivery_time'] = 'N/A'
        del_els = await page.locator('.delivery-information').all()
        for el in del_els:
            if await el.is_visible():
                txt = await el.inner_text()
                if txt and txt.strip():
                    data['delivery_time'] = txt.strip()
                    break
    except:
        data['delivery_time'] = 'N/A'

    try:
        p_num = page.locator('.product-detail-ordernumber')
        data['product_number'] = (await p_num.inner_text()).strip() if await p_num.count() > 0 else 'N/A'
    except:
        data['product_number'] = 'N/A'

    try:
        imgs = await page.locator('.gallery-slider-thumbnails-item img').all()
        srcs = []
        for img in imgs:
            src = await img.get_attribute('src')
            if src: srcs.append(src)
        data['images'] = '; '.join(srcs) if srcs else 'N/A'
    except:
        data['images'] = 'N/A'

    return data


# === HLAVNÍ SCRAPOVACÍ LOGIKA ===

async def scrape_product(context, url, semaphore):
    """Zpracuje jeden produkt."""
    async with semaphore:
        page = await context.new_page()
        all_rows = []

        try:
            await page.route("**/*.{png,jpg,jpeg,svg,css,woff,woff2}", lambda route: route.abort())

            dbg(f"Otevírám: {url}")
            await page.goto(url, timeout=90000, wait_until="domcontentloaded")

            try:
                await page.add_style_tag(
                    content="#usercentrics-root { display: none !important; visibility: hidden !important; pointer-events: none !important; }")
                await page.evaluate(
                    "() => { const el = document.getElementById('usercentrics-root'); if (el) el.remove(); }")
            except Exception:
                pass

            if "maintenance" in page.url or await page.locator("text=Maintenance mode").count() > 0:
                raise RuntimeError("Maintenance Mode")

            base_name_el = page.locator('.product-detail-name')
            base_name = (await base_name_el.inner_text()).strip() if await base_name_el.count() > 0 else 'N/A'

            cats = []
            crumbs = await page.locator('.breadcrumb-item a.breadcrumb-link').all()
            for c in crumbs:
                title = await c.get_attribute('title')
                if title and 'home' not in title.lower():
                    text = await c.locator('.breadcrumb-title').inner_text()
                    cats.append(text.strip())
            cat_path = ' > '.join(cats)

            desc_props = await get_description_and_properties(page)

            base_data_template = {
                'base_name': base_name,
                'category_path': cat_path,
                'description_and_properties': desc_props,
                'product_name': base_name
            }

            radios = page.locator('.product-detail-configurator-option input[type="radio"]')
            count = await radios.count()

            if count == 0:
                row_data = await extract_variant_data(page, base_data_template)
                all_rows.append(row_data)
            else:
                dbg(f"Nalezeno {count} variant pro {base_name}")
                for i in range(count):
                    radio = radios.nth(i)
                    input_id = await radio.get_attribute("id")
                    label = page.locator(f'label[for="{input_id}"]')

                    await label.scroll_into_view_if_needed()
                    await label.click(force=True)
                    await page.wait_for_timeout(200)

                    is_checked = await radio.evaluate("el => el.checked")
                    if not is_checked:
                        await radio.evaluate("el => el.click()")
                        await page.wait_for_timeout(300)

                    await page.wait_for_timeout(400)

                    row_data = await extract_variant_data(page, base_data_template)

                    if row_data.get('price') == 'N/A' and row_data.get('net_price') == 'N/A':
                        await page.wait_for_timeout(500)
                        row_data = await extract_variant_data(page, base_data_template)

                    all_rows.append(row_data)

            final_rows = []
            for d in all_rows:
                price_invalid = d.get('price') in ['N/A', 'Check Site', '']
                stock_invalid = d.get('stock_status') in ['N/A', '']

                if price_invalid and stock_invalid:
                    continue

                final_rows.append([
                    d.get('base_name', ''),
                    d.get('condition', ''),
                    d.get('price', ''),
                    d.get('net_price', ''),
                    d.get('stock_status', ''),
                    d.get('quantity_available', ''),
                    d.get('delivery_time', ''),
                    d.get('product_number', ''),
                    d.get('images', ''),
                    d.get('description_and_properties', ''),
                    d.get('category_path', '')
                ])

            return final_rows, url

        except Exception as e:
            dbg(f"Chyba při zpracování {url}: {e}")
            return [], url
        finally:
            await page.close()


async def get_listing_urls(page: Page, section_url, page_num):
    parsed = urlparse(section_url)
    q = dict(parse_qsl(parsed.query))
    q["p"] = str(page_num)
    new_q = urlencode(q, doseq=True)
    target_url = urlunparse(parsed._replace(query=new_q))

    await page.goto(target_url, timeout=30000)

    alert = page.locator("div.alert.alert-danger .alert-content")
    if await alert.count() > 0:
        txt = await alert.inner_text()
        if "Unfortunately, something went wrong" in txt:
            return []

    links = []
    cards = await page.locator('.cms-listing-col a.product-name, .cms-listing-col a.btn-detail').all()

    for card in cards:
        href = await card.get_attribute('href')
        if href:
            if '/de/' in href:
                href = href.replace('/de/', '/en/')
            links.append(href)

    seen = set()
    unique_links = []
    for l in links:
        if l not in seen:
            unique_links.append(l)
            seen.add(l)

    return unique_links


async def get_sections(context):
    page = await context.new_page()
    await page.goto(HOMEPAGE)

    nav = page.locator("#main-navigation-menu")
    spacer = nav.locator("span.main-navigation-spacer")
    links = await spacer.locator("xpath=following-sibling::a[contains(@class, 'main-navigation-link')]").all()

    sections = {}
    for link in links:
        txt = (await link.inner_text()).strip()
        href = await link.get_attribute("href")
        if txt and href and '/en/' in href:
            sections[txt] = href

    await page.close()
    return sections


# === UKLÁDÁNÍ DAT ===
class DataWriter:
    def __init__(self, filepath, fmt):
        self.filepath = Path(filepath)
        self.fmt = fmt
        self.headers = [
            'Product Name', 'Condition', 'Price', 'Net Price', 'Stock Status',
            'Quantity Available', 'Delivery Time', 'Product Number', 'Images',
            'Description & Properties', 'Category Path'
        ]
        self._init_file()

    def _init_file(self):
        # Pokud by byl formát excel, necháme to tak, ale tento kód se primárně zaměřuje na CSV
        if self.fmt == 'excel':
            if not self.filepath.exists():
                wb = Workbook()
                ws = wb.active
                ws.append(self.headers)
                wb.save(self.filepath)
        else:
            if not self.filepath.exists():
                with open(self.filepath, 'w', newline='', encoding='utf-8') as f:
                    # === ZMĚNA: oddělovač ; a vynucené uvozovky pro vše ===
                    writer = csv.writer(f, delimiter=';', quoting=csv.QUOTE_ALL)
                    writer.writerow(self.headers)

    def write(self, rows):
        if not rows: return
        if self.fmt == 'excel':
            wb = openpyxl.load_workbook(self.filepath)
            ws = wb.active
            for r in rows:
                ws.append(r)
            wb.save(self.filepath)
            wb.close()
        else:
            with open(self.filepath, 'a', newline='', encoding='utf-8') as f:
                # === ZMĚNA: oddělovač ; a vynucené uvozovky pro vše ===
                writer = csv.writer(f, delimiter=';', quoting=csv.QUOTE_ALL)
                writer.writerows(rows)


# === HLAVNÍ FUNKCE ===
async def main():
    print("=== IT-Market Scraper (Playwright) ===")

    # === ZMĚNA: Hardcoded nastavení pro CSV, uvozovky a název souboru ===
    file_format = "csv"
    out_name = "it-market.csv"
    print(f"Výstup nastaven na: {out_name} (Formát: {file_format}, oddělovač: ';', uvozovky: vše)")

    headless = input("Headless režim? (ano/ne - enter=ano): ").strip().lower() != "ne"

    workers_input = input("Počet paralelních oken (enter=5): ").strip()
    max_concurrent = int(workers_input) if workers_input.isdigit() else 5

    max_pages_input = input("Max stránek na sekci (enter=vše): ").strip()
    max_pages = int(max_pages_input) if max_pages_input.isdigit() else None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=["--disable-gpu"])
        context = await browser.new_context(viewport={"width": 1600, "height": 1200})

        print("Načítám sekce...")
        try:
            sections = await get_sections(context)
        except Exception as e:
            print(f"Chyba při načítání sekcí: {e}")
            await browser.close()
            return

        names = list(sections.keys())
        print("\nDostupné sekce:")
        for i, n in enumerate(names, 1):
            print(f"  {i}. {n}")

        choice = input("\nVyber sekci (číslo, název nebo 'vše'): ").strip()

        selected_sections = []
        if choice.lower() in ("vse", "vše", "all"):
            selected_sections = names
        elif choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(names):
                selected_sections = [names[idx]]
        else:
            for n in names:
                if choice.lower() in n.lower():
                    selected_sections.append(n)
                    break

        if not selected_sections:
            print("Nic nevybráno.")
            await browser.close()
            return

        progress = load_progress()
        start_sec_idx = 0
        start_page = 1
        start_prod_idx = 1

        if progress:
            print(f"Nalezen progress: {progress['section']} - str {progress['page']}")
            if input("Pokračovat? (ano/ne): ").lower() == 'ano':
                if progress['section'] in selected_sections:
                    if len(selected_sections) > 1:
                        try:
                            start_sec_idx = selected_sections.index(progress['section'])
                        except ValueError:
                            pass
                    start_page = progress['page']
                    start_prod_idx = progress['product_idx_on_page']
            else:
                clear_progress()

        writer = DataWriter(out_name, file_format)
        semaphore = asyncio.Semaphore(max_concurrent)

        total_processed = 0

        for i in range(start_sec_idx, len(selected_sections)):
            sec_name = selected_sections[i]
            sec_url = sections[sec_name]
            print(f"\n>>> Zpracovávám sekci: {sec_name}")

            current_page = start_page if i == start_sec_idx else 1
            current_prod_idx = start_prod_idx if i == start_sec_idx else 1

            listing_page_obj = await context.new_page()

            while True:
                if max_pages and current_page > max_pages:
                    print("Dosažen limit stránek.")
                    break

                print(f"  > Načítám listing stranu {current_page}...")
                urls = await get_listing_urls(listing_page_obj, sec_url, current_page)

                if not urls:
                    print("  > Žádné další produkty, konec sekce.")
                    break

                urls_to_process = urls[(current_prod_idx - 1):]
                if not urls_to_process and current_prod_idx > 1:
                    break

                print(f"    > Nalezeno {len(urls)} produktů (zpracuji {len(urls_to_process)})")

                pending_tasks = []
                for idx_on_page_rel, p_url in enumerate(urls_to_process):
                    real_idx = (current_prod_idx - 1) + idx_on_page_rel + 1
                    coro = scrape_product(context, p_url, semaphore)
                    task = asyncio.create_task(coro)
                    pending_tasks.append(task)

                for completed_task in asyncio.as_completed(pending_tasks):
                    try:
                        rows, url_done = await completed_task
                        if rows:
                            writer.write(rows)
                            total_processed += 1
                            dbg(f"Hotovo ({total_processed}): {url_done}")
                    except Exception as e:
                        print(f"CHYBA v tasku: {e}")

                await save_progress(sec_name, current_page + 1, 1, "End of Page")

                current_page += 1
                current_prod_idx = 1

            await listing_page_obj.close()
            start_page = 1

        print("\n=== Hotovo ===")
        print(f"Celkem zpracováno produktů: {total_processed}")
        clear_progress()
        await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nPřerušeno uživatelem.")