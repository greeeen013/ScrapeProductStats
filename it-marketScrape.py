import re
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, StaleElementReferenceException
)

import openpyxl
from openpyxl import Workbook

import requests
from bs4 import BeautifulSoup



def dbg(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")

import re
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, StaleElementReferenceException
)

import openpyxl
from openpyxl import Workbook

import requests
from bs4 import BeautifulSoup

from webdriver_manager.chrome import ChromeDriverManager  # Fixed missing import

import platform
import shutil
from selenium.webdriver.chrome.service import Service as ChromeService

# === Cross-platform Selenium config ===
# Choose your browser without prompts:
#   "chrome"   -> Google Chrome
#   "chromium" -> Chromium (useful on Raspberry Pi OS / Debian)
BROWSER_CHOICE = "chromium"  # change to "chrome" on Windows if you prefer

# How to find chromedriver:
#   "auto"   -> use webdriver-manager (great on Windows; downloads correct driver automatically)
#   "system" -> use chromedriver from PATH or CHROMEDRIVER_PATH
DRIVER_MODE = "auto" if platform.system() == "Windows" else "system"

# Optional overrides (leave as None to auto-detect sensible defaults)
CHROMIUM_BINARY = None  # e.g., "/usr/bin/chromium" or "/usr/bin/chromium-browser"
CHROMEDRIVER_PATH = None  # e.g., "/usr/bin/chromedriver" on Raspberry Pi

def chrome_driver(headless=True,
                  browser_choice: str | None = None,
                  driver_mode: str | None = None,
                  chromium_binary: str | None = None,
                  chromedriver_path: str | None = None):
    """Create a Chrome/Chromium WebDriver that works on Windows and Raspberry Pi.

    Parameters allow selecting Chrome vs Chromium and how to locate the driver.
    They have sensible defaults from the module-level constants above.
    """
    bc = (browser_choice or BROWSER_CHOICE or "chromium").lower()
    dm = (driver_mode or DRIVER_MODE or "system").lower()
    binary_override = chromium_binary or CHROMIUM_BINARY
    driver_override = chromedriver_path or CHROMEDRIVER_PATH

    opts = webdriver.ChromeOptions()
    if headless:
        # \"new\" headless is correct for Chromium 109+
        opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--window-size=1400,1600")

    # If user wants Chromium, set binary; try to autodetect common paths on Linux
    if bc == "chromium":
        if binary_override:
            opts.binary_location = binary_override
        else:
            # try common Linux paths
            for candidate in ("/usr/bin/chromium", "/usr/bin/chromium-browser"):
                if Path(candidate).exists():
                    opts.binary_location = candidate
                    break

    # Build the Service depending on mode
    service = None
    if dm == "auto":
        try:
            service = ChromeService(ChromeDriverManager().install())
        except Exception as e:
            # Fallback to system if webdriver-manager isn't available/working
            dm = "system"

    if dm == "system":
        # Use explicit override, PATH, or common default on Linux
        path = driver_override or shutil.which("chromedriver")
        if not path and platform.system() != "Windows":
            # Typical Raspberry Pi location
            for candidate in ("/usr/bin/chromedriver", "/snap/bin/chromium.chromedriver"):
                if Path(candidate).exists():
                    path = candidate
                    break
        if not path:
            raise RuntimeError("chromedriver was not found. Install it or set CHROMEDRIVER_PATH.")
        service = ChromeService(executable_path=path)

    return webdriver.Chrome(options=opts, service=service)

# The rest of the code remains unchanged


def safe_click_variant_by_index(driver, idx, timeout=15, max_retries=3):
    attempt = 0
    while attempt < max_retries:
        attempt += 1
        try:
            radios = driver.find_elements(
                By.CSS_SELECTOR, '.product-detail-configurator-option input[type="radio"]'
            )
            if not radios:
                raise NoSuchElementException("Nenalezeny žádné varianty (radio inputs).")
            if idx >= len(radios):
                raise IndexError(f"Požadovaný index {idx} mimo rozsah ({len(radios)} variant).")

            radio = radios[idx]
            input_id = radio.get_attribute("id")
            if not input_id:
                raise NoSuchElementException("Radio input nemá atribut id.")

            label = driver.find_element(By.CSS_SELECTOR, f'label[for="{input_id}"]')
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", label)
            WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, f'label[for="{input_id}"]'))
            )
            dbg(f"Klikám na variantu #{idx} (for={input_id}), pokus {attempt}/{max_retries}")
            driver.execute_script("arguments[0].click();", label)

            def is_checked(_):
                try:
                    r = driver.find_element(By.ID, input_id)
                    return r.is_selected() or r.get_attribute("checked") == "true"
                except StaleElementReferenceException:
                    return False

            WebDriverWait(driver, timeout).until(is_checked)
            time.sleep(0.5)
            return input_id, label
        except (StaleElementReferenceException, NoSuchElementException, TimeoutException) as e:
            dbg(f"VARIANTA retry kvůli: {type(e).__name__}: {e}")
            if attempt >= max_retries:
                raise
            time.sleep(0.6)


def _parse_price_block_text(text):
    """
    Rozparsuje blok s cenou typu:
      '€166.60*\\nNet: €140.00'
    a vrátí (price, net_price) bez hvězdiček a s ořezanými mezerami.
    """
    cleaned = text.replace('*', '').strip()
    lines = [l.strip() for l in cleaned.splitlines() if l.strip()]
    price = 'N/A'
    net_price = 'N/A'
    if lines:
        price = lines[0]
    m = re.search(r'(?i)\bnet\s*:\s*(.+)', cleaned)
    if m:
        net_price = m.group(1).strip()
    return price, net_price


def _collect_description_and_properties_from_pane(driver, timeout=15):
    import re
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    def _norm(txt: str) -> str:
        if txt is None:
            return ""
        # sjednotí whitespace, odstraní NBSP a ořeže
        txt = txt.replace("\xa0", " ")
        txt = re.sub(r"\s+", " ", txt)
        return txt.strip()

    # 1) Najdi panel a případně ho rozbal
    pane = WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.ID, "description-tab-pane"))
    )

    try:
        # pokud není rozbalený, rozbalit přes tlačítko s aria-controls
        if "show" not in (pane.get_attribute("class") or ""):
            btn = driver.find_element(By.CSS_SELECTOR, 'button[aria-controls="description-tab-pane"]')
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            btn.click()
            WebDriverWait(driver, timeout).until(
                lambda d: "show" in d.find_element(By.ID, "description-tab-pane").get_attribute("class")
            )
    except Exception:
        # když se nepodaří rozbalit, pokračujeme i tak (někdy jde text vyčíst i ze skrytého obsahu)
        pass

    # 2) Popisek – primárně z .product-detail-description-text, fallback z tlačítka "Description ..."
    description = ""
    try:
        desc_elem = pane.find_element(By.CSS_SELECTOR, ".product-detail-description-text")
        description = _norm(desc_elem.get_attribute("textContent"))
    except Exception:
        description = ""

    if not description:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, 'button[aria-controls="description-tab-pane"]')
            btn_text = _norm(btn.get_attribute("textContent"))
            # odeber úvodní "Description"
            if btn_text.lower().startswith("description"):
                description = _norm(btn_text[len("description"):])
            else:
                description = btn_text
        except Exception:
            description = ""

    # 3) Parametry: projít všechny řádky tabulky
    properties = []
    try:
        rows = pane.find_elements(By.CSS_SELECTOR, "table.product-detail-properties-table tr.properties-row")
        for row in rows:
            try:
                label_elem = row.find_element(By.CSS_SELECTOR, ".properties-label")
                value_elem = row.find_element(By.CSS_SELECTOR, ".properties-value")

                label = _norm(label_elem.get_attribute("textContent")).rstrip(":")
                value = _norm(value_elem.get_attribute("textContent"))

                if label and value:
                    properties.append(f"{label}: {value}")
            except Exception:
                continue
    except Exception:
        pass

    # 4) Sestavit výsledek: popisek; Klíč: Hodnota; Klíč: Hodnota; ...
    parts = [description] if description else []
    parts.extend(properties)
    return "; ".join(parts)



def extract_variant_data(driver):
    """
    Vrací dict bez číslovaného názvu:
      base_name, variant_type, price, net_price, stock_status, quantity_available,
      delivery_time, product_number, images, description_and_properties, category_path
    """
    data = {}

    # vybraná varianta – typ + ceny + stock
    try:
        checked = driver.find_element(By.CSS_SELECTOR, 'input[type="radio"]:checked')
        label = driver.find_element(By.CSS_SELECTOR, f'label[for="{checked.get_attribute("id")}"]')

        # typ/label title
        variant_title_el = label.find_element(By.CLASS_NAME, 'product-detail-configurator-option-label-title')
        data['variant_type'] = variant_title_el.text.split('\n')[0].strip()

        # ceny
        price_block = None
        try:
            price_block = label.find_element(By.CLASS_NAME, 'product-detail-configurator-option-label-prices')
        except NoSuchElementException:
            price_block = driver.find_element(By.CLASS_NAME, 'product-detail-configurator-option-label-prices')

        price_text = price_block.text if price_block else ''
        price, net_price = _parse_price_block_text(price_text)
        dbg(f"CENA raw: '{price_text}' -> price='{price}' | net='{net_price}'")
        data['price'] = price
        data['net_price'] = net_price

        # stock/delivery u varianty
        stock_elem = label.find_elements(By.CLASS_NAME, 'product-detail-configurator-option-inStock')
        delivery_elem = label.find_elements(By.CLASS_NAME, 'product-detail-configurator-option-withDelivery')
        data['stock_status'] = stock_elem[0].text if stock_elem else (delivery_elem[0].text if delivery_elem else 'N/A')
    except NoSuchElementException:
        data.update({'variant_type': 'N/A', 'price': 'N/A', 'net_price': 'N/A', 'stock_status': 'N/A'})

    # název produktu (bez číslování)
    try:
        data['base_name'] = driver.find_element(By.CLASS_NAME, 'product-detail-name').text.strip()
    except NoSuchElementException:
        data['base_name'] = 'N/A'

    # množství skladem
    try:
        data['quantity_available'] = driver.find_element(By.CLASS_NAME, 'product-detail-quantity-available').text
    except NoSuchElementException:
        data['quantity_available'] = 'N/A'

    # dodací doba
    try:
        el = driver.find_element(By.CSS_SELECTOR, '.delivery-information.delivery-available, .delivery-information')
        data['delivery_time'] = el.text.strip()
    except NoSuchElementException:
        data['delivery_time'] = 'N/A'

    # číslo produktu
    try:
        data['product_number'] = driver.find_element(By.CLASS_NAME, 'product-detail-ordernumber').text.strip()
    except NoSuchElementException:
        data['product_number'] = 'N/A'

    # obrázky
    try:
        images = driver.find_elements(By.CSS_SELECTOR, '.gallery-slider-thumbnails-item img')
        data['images'] = '; '.join([img.get_attribute('src') for img in images if img.get_attribute('src')])
    except NoSuchElementException:
        data['images'] = 'N/A'

    # description + properties **jen z description panelu**
    data['description_and_properties'] = _collect_description_and_properties_from_pane(driver)

    # breadcrumbs
    try:
        breadcrumbs = driver.find_elements(By.CSS_SELECTOR, '.breadcrumb-item a.breadcrumb-link')
        categories = []
        for crumb in breadcrumbs:
            try:
                title = (crumb.get_attribute('title') or '').lower()
                if 'home' in title:
                    continue
                categories.append(crumb.find_element(By.CLASS_NAME, 'breadcrumb-title').text.strip())
            except Exception:
                continue
        data['category_path'] = ' > '.join([c for c in categories if c])
    except NoSuchElementException:
        data['category_path'] = 'N/A'

    dbg(f"→ {data.get('base_name','N/A')} | {data.get('variant_type','N/A')} | {data.get('price','N/A')} | {data.get('net_price','N/A')}")
    return data


def scrape_product_data(url, excel_file='product_data.xlsx', headless=True):
    # /de/ -> /en/
    parsed = urlparse(url)
    if '/de/' in parsed.path:
        parsed = parsed._replace(path=parsed.path.replace('/de/', '/en/'))
        url = urlunparse(parsed)

    driver = chrome_driver(headless=headless)
    dbg(f"Otevírám URL: {url}")

    try:
        driver.get(url)
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CLASS_NAME, "product-detail-configurator-options"))
        )
        dbg("Stránka načtena, varianty viditelné.")

        # Excel
        try:
            wb = openpyxl.load_workbook(excel_file)
            sheet = wb.active
            dbg(f"Načítám existující Excel: {excel_file}")
        except FileNotFoundError:
            wb = Workbook()
            sheet = wb.active
            headers = [
                'Product Name', 'Variant Type', 'Price', 'Net Price', 'Stock Status',
                'Quantity Available', 'Delivery Time', 'Product Number', 'Images',
                'Description & Properties', 'Category Path'
            ]
            sheet.append(headers)
            dbg(f"Vytvářím nový Excel: {excel_file}")

        # kolik variant?
        radios = driver.find_elements(By.CSS_SELECTOR, '.product-detail-configurator-option input[type="radio"]')
        total = max(1, len(radios))
        dbg(f"Nalezeno variant: {total}")

        # === mapování pro číslování DLE DUPLICIT ===
        # klíč: f"{base_name}_{variant_type}" -> počet výskytů
        name_counts = {}

        for i in range(total):
            dbg(f"=== Zpracovávám variantu {i+1}/{total} ===")
            try:
                input_id, _label = safe_click_variant_by_index(driver, i)
                dbg(f"Vybraná varianta input_id={input_id}")
            except Exception as e:
                dbg(f"Chyba při výběru varianty {i+1}: {e}")

            try:
                d = extract_variant_data(driver)
            except Exception as e:
                dbg(f"Chyba při čtení dat varianty {i+1}: {e}")
                d = {
                    'base_name': 'N/A', 'variant_type': 'N/A', 'price': 'N/A', 'net_price': 'N/A',
                    'stock_status': 'N/A', 'quantity_available': 'N/A', 'delivery_time': 'N/A',
                    'product_number': 'N/A', 'images': 'N/A', 'description_and_properties': 'N/A',
                    'category_path': 'N/A'
                }

            # === vytvoření Product Name s číslováním jen u duplicit ===
            base = d.get('base_name', 'N/A')
            vtype = d.get('variant_type', 'N/A')
            key = f"{base}_{vtype}"
            name_counts[key] = name_counts.get(key, 0) + 1
            if name_counts[key] == 1:
                product_name = key
            else:
                product_name = f"{key}_{name_counts[key]}"
            d['product_name'] = product_name
            dbg(f"NAME key='{key}' -> '{product_name}' (count={name_counts[key]})")

            row = [
                d.get('product_name', ''),
                d.get('variant_type', ''),
                d.get('price', ''),
                d.get('net_price', ''),
                d.get('stock_status', ''),
                d.get('quantity_available', ''),
                d.get('delivery_time', ''),
                d.get('product_number', ''),
                d.get('images', ''),
                d.get('description_and_properties', ''),
                d.get('category_path', '')
            ]
            sheet.append(row)

        wb.save(excel_file)
        dbg(f"Data uložena do {excel_file}")

    finally:
        dbg("Ukončuji prohlížeč.")
        driver.quit()

HOMEPAGE = "https://it-market.com/en"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/123.0.0.0 Safari/537.36"
}

def _soup_get(url: str, timeout: int = 20) -> BeautifulSoup:
    """GET → BeautifulSoup s rozumnými hlavičkami a timeoutem."""
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

def _after_pipe_product_sections(homepage_url: str = HOMEPAGE):
    """
    Vrátí Ordered dict {název: url} pro položky za svislou čárou v hlavní navigaci.
    Ignoruje Manufacturer / Services / Blog před '|'.
    """
    soup = _soup_get(homepage_url)
    nav = soup.find("nav", id="main-navigation-menu")
    if not nav:
        raise RuntimeError("Nenalezena hlavní navigace (nav#main-navigation-menu).")

    # Najdi '|' (span.main-navigation-spacer) a vezmi všechny následující <a.nav-link.main-navigation-link>
    spacer = nav.find("span", class_="main-navigation-spacer")
    if not spacer:
        raise RuntimeError("Nenalezen oddělovač '|' v navigaci.")

    sections = {}
    for a in spacer.find_all_next("a", class_="main-navigation-link"):
        txt = a.get_text(strip=True)
        href = a.get("href")
        if not href or not txt:
            continue
        # sekce jsou top-level kategorické odkazy (např. /en/switches, /en/router, …)
        # pro jistotu filtruj na relativně krátké slugy (bez dalších podkategorií v URL)
        try:
            p = urlparse(href)
            path_parts = [x for x in p.path.split("/") if x]
            # očekáváme něco jako /en/switches nebo /en/router atd. → 2 nebo 3 části
            # ['en','switches'] == 2 části
            if len(path_parts) == 2 and path_parts[0] == "en":
                sections[txt] = href
        except Exception:
            continue

    if not sections:
        raise RuntimeError("Za '|' nebyly nalezeny žádné produktové sekce.")

    return sections

def _iter_listing_pages(section_url: str, start_page: int = 1, max_pages: int | None = None):
    """
    Generátor vracející (page_number, soup) pro listing stránky sekce.
    Končí, když:
      - nenajde produktový wrapper, nebo
      - se objeví chybová hláška 'Unfortunately, something went wrong.', nebo
      - dojde na max_pages (pokud je nastaveno).
    """
    page = max(1, start_page)
    while True:
        # přidej / uprav ?p=N v URL
        parsed = urlparse(section_url)
        q = dict(parse_qsl(parsed.query))
        q["p"] = str(page)
        new_q = urlencode(q, doseq=True)
        page_url = urlunparse(parsed._replace(query=new_q))

        soup = _soup_get(page_url)

        # chyba stránky (když přejedeme počet stránek)
        err = soup.select_one("div.alert.alert-danger .alert-content")
        if err and "Unfortunately, something went wrong" in err.get_text(strip=True):
            break

        wrapper = soup.find("div", class_="row cms-listing-row js-listing-wrapper", attrs={"role": "list"})
        items = wrapper.find_all("div", class_="cms-listing-col") if wrapper else []
        if not items:
            # nic k zobrazení => konec
            break

        yield page, soup

        page += 1
        if max_pages is not None and page > max_pages:
            break

def _extract_product_links(listing_soup: BeautifulSoup) -> list[str]:
    """
    Z listingu vytáhne URL na detail produktu.
    Hledá <a class="product-name stretched-link" href="...">, fallback na 'Details' tlačítko.
    """
    urls = []
    wrapper = listing_soup.find("div", class_="row cms-listing-row js-listing-wrapper", attrs={"role": "list"})
    if not wrapper:
        return urls

    cards = wrapper.find_all("div", class_="cms-listing-col", attrs={"role": "listitem"})
    for card in cards:
        a = card.select_one("a.product-name.stretched-link")
        if not a:
            a = card.select_one("a.btn.btn-primary.btn-detail")
        if a and a.get("href"):
            urls.append(a["href"])
    return urls

def _normalize_en_url(url: str) -> str:
    """Jistota, že používáme /en/ (detaily mohou být /de/ → přepneme na /en/)."""
    parsed = urlparse(url)
    path = parsed.path.replace("/de/", "/en/")
    return urlunparse(parsed._replace(path=path))

def run_it_market_scraper(excel_file: str = "product_data.xlsx",
                          headless: bool = True,
                          delay_between_requests: float = 0.6,
                          max_pages_per_section: int | None = None):
    """
    Nadstavbová funkce:
      1) načte sekce za '|'
      2) v konzoli nabídne volbu (název sekce nebo 'vše')
      3) projde listing vybrané/ých sekcí
      4) pro každý produkt zavolá scrape_product_data(url, excel_file, headless)
      5) stránkuje přes ?p=2, ?p=3, ...
    """

    print("[it-market] Čtu sekce z hlavní stránky…")
    sections = _after_pipe_product_sections(HOMEPAGE)
    names = list(sections.keys())

    print("\nDostupné sekce (za '|'):")
    for i, n in enumerate(names, 1):
        print(f"  {i}. {n}")
    print("  * napiš 'vše' pro zpracování všech sekcí")

    choice = input("\nCo chceš zpracovat? (název sekce nebo 'vše'): ").strip()

    # vyber seznam (vše vs. konkrétní)
    if choice.lower() in ("vse", "vše", "all", "everything"):
        picked = names
    else:
        # dovolíme zadat název nebo index
        if choice.isdigit():
            idx = int(choice)
            if not (1 <= idx <= len(names)):
                print("Neplatný index sekce.")
                return
            picked = [names[idx - 1]]
        else:
            # case-insensitive match jména
            matching = [n for n in names if n.lower() == choice.lower()]
            if not matching:
                # zkuste částečnou shodu
                matching = [n for n in names if choice.lower() in n.lower()]
            if not matching:
                print("Sekce nenalezena.")
                return
            picked = [matching[0]]

    seen_urls = set()

    for sec_name in picked:
        sec_url = sections[sec_name]
        print(f"\n=== Sekce: {sec_name} → {sec_url} ===")

        total_products = 0
        for page_num, soup in _iter_listing_pages(sec_url, start_page=1, max_pages=max_pages_per_section):
            print(f"[{sec_name}] Stránka {page_num}…")
            product_links = _extract_product_links(soup)
            if not product_links:
                print(f"[{sec_name}] Na stránce {page_num} nejsou žádné produkty → končím sekci.")
                break

            for idx_on_page,link in enumerate(product_links, start=1):
                url = _normalize_en_url(link)
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                print(f"  → stránka: {page_num}")
                print(f"  → produkt: {idx_on_page}/24")
                print(f"  → url: {url}")
                try:
                    # Volání existující funkce na detail (selenium), Excel appenduje sama
                    scrape_product_data(url, excel_file=excel_file, headless=headless)
                except Exception as e:
                    print(f"    ! chyba při zpracování produktu: {e}")

                total_products += 1
                if delay_between_requests:
                    time.sleep(delay_between_requests)

        print(f"[{sec_name}] Hotovo. Zpracováno produktů: {total_products}")


if __name__ == "__main__":
    # Příklad běhu nadstavby:
    # - nechá tě vybrat sekci nebo 'vše'
    # - zapisuje do product_data.xlsx
    # - product detaily se otevírají v headless Chromu
    run_it_market_scraper(
        excel_file="product_data.xlsx",
        headless=True,
        delay_between_requests=0.5,
        max_pages_per_section=None  # nebo např. 3 pro rychlý test
    )
