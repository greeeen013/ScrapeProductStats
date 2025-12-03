import asyncio
import csv
import re
import time
import random
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl, urljoin

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError

# === KONFIGURACE ===
BASE_URL = "https://smicro.cz"
START_URL = "https://smicro.cz"


# === POMOCNÉ FUNKCE ===
def dbg(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def clean_text(text):
    if not text:
        return "N/A"
    text = str(text).replace(';', ',').replace('"', "'")
    text = re.sub(r'[\r\n\t]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip() or "N/A"


def parse_price(text):
    if not text:
        return "N/A"
    cleaned = re.sub(r'[^\d,.]', '', text).strip()
    return cleaned


# === UKLÁDÁNÍ DO CSV ===
class CsvWriter:
    def __init__(self, filepath):
        self.filepath = Path(filepath)
        if not self.filepath.exists():
            with open(self.filepath, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f, delimiter=';', quoting=csv.QUOTE_ALL)
                writer.writerow([
                    'Index',
                    'Product Name',
                    'Variant Type',
                    'Part Number',
                    'Manufacturer',
                    'Availability Local',
                    'Availability Supplier',
                    'Price No VAT',
                    'Price With VAT',
                    'Specifications & Desc',
                    'Product URL'
                ])

        self.current_index = 0
        if self.filepath.exists():
            try:
                with open(self.filepath, 'r', encoding='utf-8-sig') as f:
                    rows = list(csv.reader(f, delimiter=';'))
                    if len(rows) > 1:
                        try:
                            self.current_index = int(rows[-1][0])
                        except:
                            pass
            except:
                pass

    def write(self, rows):
        if not rows: return
        with open(self.filepath, 'a', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f, delimiter=';', quoting=csv.QUOTE_ALL)
            for row in rows:
                self.current_index += 1
                full_row = [self.current_index] + [str(item) if item is not None else "N/A" for item in row]
                writer.writerow(full_row)


# === LOGIKA WEBU SMICRO.CZ ===

async def get_categories(context):
    """Načte kategorie z homepage s retry logikou."""
    page = await context.new_page()
    dbg("Načítám kategorie...")

    # Zkusíme načíst homepage až 3x
    for attempt in range(3):
        try:
            # Delší timeout pro první načtení
            await page.goto(START_URL, wait_until="domcontentloaded", timeout=90000)
            break
        except Exception as e:
            if attempt == 2:
                dbg(f"Kritická chyba při načítání kategorií: {e}")
                await page.close()
                return {}
            dbg(f"Pokus {attempt + 1} selhal, zkouším znovu za 5s...")
            await asyncio.sleep(5)

    try:
        categories = {}
        # Selektor pro kategorie vlevo nebo na HP
        links = await page.locator('.categories.HPcategories ol li a').all()

        for link in links:
            desc_el = link.locator('.mDesc')
            if await desc_el.count() > 0:
                name = await desc_el.inner_text()
                href = await link.get_attribute('href')
                if name and href:
                    full_url = urljoin(BASE_URL, href)
                    categories[clean_text(name)] = full_url
        return categories
    finally:
        await page.close()


async def get_listing_product_urls(page: Page, category_url, page_num):
    target_url = category_url
    separator = "&" if "?" in category_url else "?"

    if page_num > 0:
        if "page=" in target_url:
            target_url = re.sub(r'page=\d+', f'page={page_num}', target_url)
        else:
            target_url = f"{target_url}{separator}page={page_num}"

    dbg(f"  > Listing page {page_num}: {target_url}")

    for attempt in range(3):
        try:
            await page.goto(target_url, timeout=60000, wait_until="domcontentloaded")

            container = page.locator('#productAjaxPagerContainer')
            if await container.count() > 0:
                if await container.locator('.item').count() == 0:
                    return []

            links = []
            items = await page.locator('#productAjaxPagerContainer .item h3 a').all()

            for item in items:
                href = await item.get_attribute('href')
                if href:
                    links.append(urljoin(BASE_URL, href))

            return list(set(links))
        except Exception as e:
            if attempt == 2:
                dbg(f"Chyba listingu po 3 pokusech: {e}")
                return []
            dbg(f"    Chyba načítání listingu (pokus {attempt + 1}), zkouším znovu...")
            await asyncio.sleep(2)

    return []


async def extract_product_data(page: Page, url, variant_name="Standard"):
    data = {
        'name': 'N/A',
        'variant': variant_name,
        'part_number': 'N/A',
        'manufacturer': 'N/A',
        'avail_local': 'N/A',
        'avail_supplier': 'N/A',
        'price_no_vat': 'N/A',
        'price_vat': 'N/A',
        'specs': 'N/A',
        'url': url
    }

    try:
        # Čekáme na H1, což značí, že se stránka vykreslila
        await page.wait_for_selector('h1', timeout=15000)

        h1 = page.locator('h1').first
        if await h1.count() > 0:
            data['name'] = clean_text(await h1.inner_text())

        rows = await page.locator('table.tabData tr').all()
        for row in rows:
            th = row.locator('th')
            td = row.locator('td')

            if await th.count() > 0 and await td.count() > 0:
                header = clean_text(await th.inner_text()).lower()
                val = clean_text(await td.inner_text())

                if "part number" in header:
                    data['part_number'] = val
                elif "kód produktu" in header and data['part_number'] == 'N/A':
                    data['part_number'] = val

                if "výrobce" in header:
                    data['manufacturer'] = val

                if "dostupnost u nás" in header:
                    u_tag = td.locator('u')
                    if await u_tag.count() > 0:
                        data['avail_local'] = clean_text(await u_tag.inner_text())
                    else:
                        data['avail_local'] = val

                if "dostupnost u dodavatele" in header:
                    u_tag = td.locator('u')
                    if await u_tag.count() > 0:
                        data['avail_supplier'] = clean_text(await u_tag.inner_text())
                    else:
                        data['avail_supplier'] = val

        price_box = page.locator('.detPrice').first
        if await price_box.count() > 0:
            no_vat = price_box.locator('.cena strong')
            if await no_vat.count() > 0:
                data['price_no_vat'] = parse_price(await no_vat.inner_text())

            with_vat = price_box.locator('.cenaDph strong')
            if await with_vat.count() > 0:
                data['price_vat'] = parse_price(await with_vat.inner_text())

        desc_parts = []
        # Opravený selektor pro popis, aby nebral <hr>
        desc_div = page.locator('div#popis').first
        if await desc_div.count() > 0:
            full_text = await desc_div.inner_text()
            desc_parts.append(clean_text(full_text[:1200]))

        param_rows = await page.locator('table.tabParam tr').all()
        specs_list = []
        for pr in param_rows:
            th = pr.locator('th')
            td = pr.locator('td')
            if await th.count() > 0 and await td.count() > 0:
                k = clean_text(await th.inner_text())
                v = clean_text(await td.inner_text())
                specs_list.append(f"{k}: {v}")

        if specs_list:
            desc_parts.append("PARAMETRY: " + " | ".join(specs_list))

        data['specs'] = " ; ".join(desc_parts)

    except Exception as e:
        dbg(f"Chyba extrakce dat {url}: {e}")

    return data


async def scrape_product(context, url, semaphore):
    async with semaphore:
        # Zvýšený náhodný delay pro bezpečnost
        await asyncio.sleep(random.uniform(1.0, 3.0))

        page = await context.new_page()
        all_extracted = []

        try:
            # Neblokujeme nic, aby se stránka načetla přirozeně a nevypadalo to podezřele
            # Maximálně můžeme blokovat obrázky, pokud server nehlídá fingerprinting
            await page.route("**/*.{png,jpg,jpeg,gif,webp}", lambda route: route.abort())

            loaded = False
            for attempt in range(3):
                try:
                    await page.goto(url, timeout=90000, wait_until="commit")
                    loaded = True
                    break
                except Exception as e:
                    dbg(f"    Timeout/Error ({url}) pokus {attempt + 1}: {str(e)[:50]}...")
                    await asyncio.sleep(3)

            if not loaded:
                dbg(f"SKIP: Nepodařilo se načíst {url}")
                return []

            base_data = await extract_product_data(page, url)
            all_extracted.append(base_data)

            rows_to_return = []
            for d in all_extracted:
                rows_to_return.append([
                    d['name'],
                    d['variant'],
                    d['part_number'],
                    d['manufacturer'],
                    d['avail_local'],
                    d['avail_supplier'],
                    d['price_no_vat'],
                    d['price_vat'],
                    d['specs'],
                    d['url']
                ])

            return rows_to_return

        except Exception as e:
            dbg(f"Kritická chyba produktu {url}: {e}")
            return []
        finally:
            await page.close()


# === HLAVNÍ SMYČKA ===
async def main():
    print("=== SMICRO.CZ Scraper (Headful Version) ===")
    csv_name = "smicro_products.csv"

    print("DOPORUČENÍ: Pro stabilitu zadejte max 2 nebo 3 workery.")
    w_input = input("Počet workerů (okno) [2]: ").strip()
    max_concurrent = int(w_input) if w_input.isdigit() else 2

    async with async_playwright() as p:
        # ZMĚNA: headless=False -> otevře se fyzické okno prohlížeče
        # args: maskování automatizace
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            viewport={"width": 1400, "height": 900}
        )

        # Načtení kategorií
        categories = await get_categories(context)

        if not categories:
            print("Nepodařilo se načíst žádné kategorie. Zkontrolujte připojení.")
            await browser.close()
            return

        print("\nNalezené kategorie:")
        cat_names = list(categories.keys())
        for i, name in enumerate(cat_names, 1):
            print(f"  {i}. {name}")

        print("\n  X. Vložit vlastní URL kategorie")

        choice = input("\nVyber možnost (číslo, 'vse' nebo 'X'): ").strip()

        urls_to_scrape = []

        if choice.lower() == 'x':
            custom_url = input("Vlož URL kategorie: ").strip()
            urls_to_scrape.append(("Custom URL", custom_url))
        elif choice.lower() in ('vse', 'all'):
            for name in cat_names:
                urls_to_scrape.append((name, categories[name]))
        elif choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(cat_names):
                name = cat_names[idx]
                urls_to_scrape.append((name, categories[name]))
        else:
            print("Neplatná volba.")
            await browser.close()
            return

        writer = CsvWriter(csv_name)
        semaphore = asyncio.Semaphore(max_concurrent)

        total_products = 0

        for cat_name, cat_url in urls_to_scrape:
            print(f"\n>>> Zpracovávám kategorii: {cat_name}")

            list_page = await context.new_page()
            curr_page_num = 1

            while True:
                product_urls = await get_listing_product_urls(list_page, cat_url, curr_page_num)

                if not product_urls:
                    print(f"  > Strana {curr_page_num} je prázdná. Konec kategorie.")
                    break

                print(f"  > Strana {curr_page_num}: Nalezeno {len(product_urls)} produktů. Zpracovávám...")

                tasks = []
                for u in product_urls:
                    tasks.append(asyncio.create_task(scrape_product(context, u, semaphore)))

                for res in asyncio.as_completed(tasks):
                    rows = await res
                    if rows:
                        writer.write(rows)
                        total_products += 1

                curr_page_num += 1

            await list_page.close()

        print(f"\n=== HOTOVO ===")
        print(f"Celkem uloženo produktů: {total_products}")
        await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nUkončeno.")