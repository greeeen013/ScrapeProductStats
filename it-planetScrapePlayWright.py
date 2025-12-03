import asyncio
import csv
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl, urljoin

from playwright.async_api import async_playwright, Page

# === KONFIGURACE ===
BASE_URL = "https://it-planet.com"
START_URL = "https://it-planet.com/en"
SCRIPT_DIR = Path(__file__).resolve().parent
PROGRESS_FILE = SCRIPT_DIR / "it-planet_progress_v6.json"


# === POMOCNÉ FUNKCE ===
def dbg(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


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
    return text.replace('€', '').replace('*', '').strip()


# === UKLÁDÁNÍ DO CSV ===
class CsvWriter:
    def __init__(self, filepath):
        self.filepath = Path(filepath)
        if not self.filepath.exists():
            with open(self.filepath, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f, delimiter=',', quoting=csv.QUOTE_ALL)
                writer.writerow([
                    'Product Name', 'Condition', 'Price', 'Delivery Time',
                    'Supplier Number', 'Product ID (SKU)', 'Images',
                    'Description', 'Category Path', 'Product URL'
                ])

    def write(self, rows):
        if not rows: return
        with open(self.filepath, 'a', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f, delimiter=',', quoting=csv.QUOTE_ALL)
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
                data['price'] = clean_text(await meta.get_attribute("content"))
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

            if url and url not in srcs:
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
            # Necháme CSS běžet
            await page.goto(url, timeout=60000, wait_until="domcontentloaded")

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
            dbg(f"CHYBA {url}: {e}")
            return [], url
        finally:
            await page.close()


# === PROCHÁZENÍ KATEGORIÍ ===

async def get_sections(context):
    page = await context.new_page()
    dbg("Načítám menu...")
    await page.goto(START_URL, wait_until="domcontentloaded")

    sections = {}
    ignored = ["blog", "service", "inquiry", "home", "brands", "manufacturer"]

    menu_items = await page.locator('.navigation--list .navigation--entry .navigation--link').all()

    for item in menu_items:
        title = await item.get_attribute("title")
        href = await item.get_attribute("href")

        if title and href:
            clean_title = title.strip()
            if clean_title.lower() not in ignored and "SupplierModified" not in href:
                full_url = urljoin(BASE_URL, href)
                sections[clean_title] = full_url

    await page.close()
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
        await page.goto(target_url, timeout=60000, wait_until="networkidle")

        try:
            await page.wait_for_selector('.product--box', timeout=5000)
        except:
            if await page.locator(".alert.is--info").count() > 0:
                return []
            pass

        links = []
        buttons = await page.locator('.product--box .product--detail-btn a').all()
        for btn in buttons:
            href = await btn.get_attribute('href')
            if href: links.append(href)

        if not links:
            titles = await page.locator('.product--box .product--title').all()
            for t in titles:
                href = await t.get_attribute('href')
                if href: links.append(href)

        return list(set(links))

    except Exception as e:
        dbg(f"Chyba listingu: {e}")
        return []


# === MAIN ===
async def main():
    print("=== IT-Planet Scraper (V6 - Images Fixed) ===")

    out_name = "it_planet_data.csv"

    w_input = input("Počet paralelních oken (doporučeno 3-5) [3]: ").strip()
    max_concurrent = int(w_input) if w_input.isdigit() else 3

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1600, "height": 1000})

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

        writer = CsvWriter(out_name)
        semaphore = asyncio.Semaphore(max_concurrent)

        total_cnt = 0

        for sec_name in selected:
            sec_url = sections[sec_name]
            print(f"\n>>> Zpracovávám: {sec_name}")

            page_obj = await context.new_page()
            curr_page = 1

            while True:
                urls = await get_listing_urls(page_obj, sec_url, curr_page)
                if not urls:
                    print(f"  > Konec {sec_name} (str {curr_page} bez produktů)")
                    break

                print(f"  > Strana {curr_page}: {len(urls)} produktů. Zpracovávám...")

                tasks = []
                for u in urls:
                    tasks.append(asyncio.create_task(scrape_product(context, u, semaphore)))

                for res in asyncio.as_completed(tasks):
                    rows, _ = await res
                    if rows:
                        writer.write(rows)
                        total_cnt += 1

                curr_page += 1

            await page_obj.close()

        print(f"\nHOTOVO. Celkem: {total_cnt}")
        await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStop.")