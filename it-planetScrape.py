import csv
import json
import re
import time
from datetime import datetime
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

from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service as ChromeService

import platform
import shutil
from requests.exceptions import HTTPError, Timeout, ConnectionError as ReqConnError


def dbg(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


# === Cross-platform Selenium config ===
BROWSER_CHOICE = "chromium"
DRIVER_MODE = "auto" if platform.system() == "Windows" else "system"
CHROMIUM_BINARY = None
CHROMEDRIVER_PATH = None


def chrome_driver(headless=True,
                  browser_choice: str | None = None,
                  driver_mode: str | None = None,
                  chromium_binary: str | None = None,
                  chromedriver_path: str | None = None):
    """Create a Chrome/Chromium WebDriver that works on Windows and Raspberry Pi."""
    bc = (browser_choice or BROWSER_CHOICE or "chromium").lower()
    dm = (driver_mode or DRIVER_MODE or "system").lower()
    binary_override = chromium_binary or CHROMIUM_BINARY
    driver_override = chromedriver_path or CHROMEDRIVER_PATH

    opts = webdriver.ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--window-size=1400,1600")

    if bc == "chromium":
        if binary_override:
            opts.binary_location = binary_override
        else:
            for candidate in ("/usr/bin/chromium", "/usr/bin/chromium-browser"):
                if Path(candidate).exists():
                    opts.binary_location = candidate
                    break

    service = None
    if dm == "auto":
        try:
            service = ChromeService(ChromeDriverManager().install())
        except Exception:
            dm = "system"

    if dm == "system":
        path = driver_override or shutil.which("chromedriver")
        if not path and platform.system() != "Windows":
            for candidate in ("/usr/bin/chromedriver", "/snap/bin/chromium.chromedriver"):
                if Path(candidate).exists():
                    path = candidate
                    break
        if not path:
            raise RuntimeError("chromedriver was not found. Install it or set CHROMEDRIVER_PATH.")
        service = ChromeService(executable_path=path)

    return webdriver.Chrome(options=opts, service=service)


def _resolve_output_path(output_file: str | Path) -> Path:
    p = Path(output_file)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def save_to_excel(data, excel_path):
    """Save data to Excel file"""
    try:
        wb = openpyxl.load_workbook(excel_path)
        sheet = wb.active
        dbg(f"Načítám existující Excel: {excel_path}")
    except FileNotFoundError:
        wb = Workbook()
        sheet = wb.active
        headers = [
            'Product Name', 'Price', 'Delivery Time', 'Supplier Number',
            'Images', 'Description', 'Category Path'
        ]
        sheet.append(headers)
        dbg(f"Vytvářím nový Excel: {excel_path}")

    sheet.append(data)
    wb.save(excel_path)


def save_to_csv(data, csv_path):
    """Save data to CSV file"""
    file_exists = csv_path.exists()

    with open(csv_path, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not file_exists:
            headers = [
                'Product Name', 'Price', 'Delivery Time', 'Supplier Number',
                'Images', 'Description', 'Category Path'
            ]
            writer.writerow(headers)
        writer.writerow(data)


HOMEPAGE = "https://it-planet.com/en"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
}

RETRY_STATUSES = {429, 500, 502, 503, 504}


class MaintenanceError(RuntimeError):
    pass


def _soup_get(url: str, timeout: int = 20, max_retries: int = 5, backoff_base: float = 0.8) -> BeautifulSoup:
    """GET s rozumnými hlavičkami, retry a detekcí /maintenance."""
    attempt = 0
    while True:
        attempt += 1
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
            if r.url.endswith("/maintenance") or r.status_code == 503:
                raise MaintenanceError(f"Maintenance mode ({r.status_code}) at {r.url}")
            r.raise_for_status()
            return BeautifulSoup(r.text, "html.parser")
        except (Timeout, ReqConnError) as e:
            if attempt > max_retries:
                raise
        except HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status not in RETRY_STATUSES or attempt > max_retries:
                raise
        except MaintenanceError:
            if attempt > max_retries:
                raise
        sleep_for = backoff_base * (2 ** (attempt - 1)) + (0.05 * attempt)
        time.sleep(sleep_for)


def get_sections_and_subsections():
    """Získá všechny sekce a podsekce z hlavního menu."""
    soup = _soup_get(HOMEPAGE)
    menu_containers = soup.find_all("div", class_="menu--container")

    sections = {}

    for container in menu_containers:
        # Získat název sekce
        button_container = container.find("div", class_="button-container")
        if not button_container:
            continue

        category_link = button_container.find("a", class_="button--category")
        if not category_link or not category_link.get("href"):
            continue

        # Extrahovat název sekce z URL
        section_url = category_link["href"]
        section_name_match = re.search(r'/c/([^/.]+)\.html', section_url)
        if not section_name_match:
            continue

        section_name = section_name_match.group(1).replace("-", " ").title()

        # Získat podsekce
        subsections = {}
        content_wrapper = container.find("div", class_="content--wrapper")
        if content_wrapper:
            menu_list = content_wrapper.find("ul", class_="menu--list")
            if menu_list:
                for item in menu_list.find_all("li", class_="menu--list-item"):
                    link = item.find("a", class_="menu--list-item-link")
                    if link and link.get("href") and link.text.strip():
                        # Extrahovat název podsekce z URL
                        subsection_url = link["href"]
                        subsection_name_match = re.search(r'/c/([^/.]+)\.html', subsection_url)
                        if subsection_name_match:
                            subsection_name = subsection_name_match.group(1).replace("-", " ").title()
                            subsections[subsection_name] = subsection_url

        sections[section_name] = {
            "url": section_url,
            "subsections": subsections
        }

    return sections


def extract_product_links(listing_soup: BeautifulSoup) -> list[str]:
    """Extrahuje odkazy na produkty z listing stránky."""
    product_links = []
    product_boxes = listing_soup.find_all("div", class_="product--box")

    for box in product_boxes:
        product_info = box.find("div", class_="product--info")
        if product_info:
            link = product_info.find("a", class_="product--title")
            if link and link.get("href"):
                product_links.append(link["href"])

    return product_links


def get_product_id_from_script(url):
    """Získá product ID ze scriptu na stránce produktu."""
    try:
        soup = _soup_get(url)

        # Hledat script tagy s datalayer
        scripts = soup.find_all("script")
        for script in scripts:
            if script.string and "dataLayer.push" in script.string:
                # Najít JSON v scriptu
                match = re.search(r'window\.dataLayer\.push\(({.*?})\);', script.string, re.DOTALL)
                if match:
                    json_str = match.group(1)
                    try:
                        data = json.loads(json_str)
                        if "ecommerce" in data and "detail" in data["ecommerce"]:
                            products = data["ecommerce"]["detail"].get("products", [])
                            if products and "id" in products[0]:
                                return products[0]["id"]
                    except json.JSONDecodeError:
                        continue

        # Fallback: zkusit najít product ID v URL nebo jiných elementech
        order_number_elem = soup.find("li", class_="entry--ordernumber")
        if order_number_elem:
            content = order_number_elem.find("span", class_="entry--content")
            if content:
                return content.text.strip()

        # Další fallback: zkusit extrahovat z URL
        match = re.search(r'/(\d+)\.html', url)
        if match:
            return match.group(1)

    except Exception as e:
        dbg(f"Chyba při získávání product ID z {url}: {e}")

    return None


def build_product_url(base_url, product_id, is_refurbished=False):
    """Sestaví správnou URL produktu s product ID."""
    parsed_url = urlparse(base_url)
    query_params = dict(parse_qsl(parsed_url.query))

    if is_refurbished:
        product_id += ".1"

    query_params["number"] = product_id
    new_query = urlencode(query_params)

    return urlunparse(parsed_url._replace(query=new_query))


def scrape_product_page(url, is_refurbished=False):
    """Scrapuje detail produktu."""
    try:
        soup = _soup_get(url)

        # Název produktu
        product_name = "N/A"
        title_elem = soup.find("h1", class_="product--title")
        if title_elem:
            product_name = title_elem.text.strip()
            if is_refurbished:
                product_name += "_refurbished"
            else:
                product_name += "_new"

        # Cena
        price = "N/A"
        price_elem = soup.find("div", class_="product--price")
        if price_elem:
            price_content = price_elem.find("span", class_="price--content")
            if price_content:
                price = price_content.text.strip()
            else:
                price_text = price_elem.text.strip()
                if "Price on request" not in price_text:
                    price = price_text

        # Dodací doba
        delivery_time = "N/A"
        delivery_elem = soup.find("p", class_="delivery--information")
        if delivery_elem:
            delivery_text = delivery_elem.find("span", class_="delivery--text")
            if delivery_text:
                delivery_time = delivery_text.text.strip().replace("\n", " ").replace("\t", " ")

        # Supplier number
        supplier_number = "N/A"
        supplier_elem = soup.find("li", class_="entry--suppliernumber")
        if supplier_elem:
            supplier_content = supplier_elem.find("span", class_="entry--content")
            if supplier_content:
                supplier_number = supplier_content.text.strip()

        # Obrázky
        images = []
        image_elems = soup.find_all("div", class_="image--box")
        for img_elem in image_elems:
            img = img_elem.find("img")
            if img and img.get("src") and "no-picture" not in img["src"]:
                images.append(img["src"])

        images_str = "; ".join(images) if images else "N/A"

        # Popisek
        description = "N/A"
        desc_elem = soup.find("div", class_="product--description")
        if desc_elem:
            # Zpracování tabulek v popisu
            tables = desc_elem.find_all("table")
            desc_parts = []

            for table in tables:
                sector = table.find_previous_sibling("h4", class_="sector")
                if sector:
                    desc_parts.append(sector.text.strip())

                rows = table.find_all("tr")
                for row in rows:
                    cells = row.find_all("td")
                    if len(cells) >= 2:
                        key = cells[0].text.strip().rstrip(":")
                        value = cells[1].text.strip()
                        desc_parts.append(f"{key}: {value}")

            if desc_parts:
                description = "; ".join(desc_parts)
            else:
                description = desc_elem.text.strip().replace("\n", " ").replace("\t", " ")

        # Kategorie
        category_path = "N/A"
        breadcrumbs = soup.find("div", class_="breadcrumb--container")
        if breadcrumbs:
            categories = []
            breadcrumb_links = breadcrumbs.find_all("a", class_="breadcrumb--link")
            for link in breadcrumb_links:
                if link.text.strip() and "Home" not in link.text:
                    categories.append(link.text.strip())

            if categories:
                category_path = " > ".join(categories)

        return {
            "product_name": product_name,
            "price": price,
            "delivery_time": delivery_time,
            "supplier_number": supplier_number,
            "images": images_str,
            "description": description,
            "category_path": category_path
        }

    except Exception as e:
        dbg(f"Chyba při scrapování produktu {url}: {e}")
        return None


def scrape_subsection(subsection_url, output_file, file_format, delay, progress_data=None):
    """Scrapuje všechny produkty v podsekci."""
    page = 1
    product_count = 0

    if progress_data and progress_data.get("subsection_url") == subsection_url:
        page = progress_data.get("page", 1)
        dbg(f"Pokračuji od stránky {page}")

    while True:
        # Sestavit URL s číslem stránky
        parsed_url = urlparse(subsection_url)
        query_params = dict(parse_qsl(parsed_url.query))
        query_params["p"] = str(page)
        new_query = urlencode(query_params)
        page_url = urlunparse(parsed_url._replace(query=new_query))

        dbg(f"Zpracovávám stránku {page}: {page_url}")

        try:
            soup = _soup_get(page_url)

            # Zkontrolovat, zda stránka obsahuje produkty
            product_boxes = soup.find_all("div", class_="product--box")
            if not product_boxes:
                dbg("Žádné další produkty nenalezeny, ukončuji podsekci.")
                break

            # Extrahovat odkazy na produkty
            product_links = extract_product_links(soup)
            if not product_links:
                dbg("Žádné odkazy na produkty nenalezeny, ukončuji podsekci.")
                break

            # Zpracovat každý produkt
            for product_link in product_links:
                # Nejprve získat product ID z původní URL
                product_id = get_product_id_from_script(product_link)

                if not product_id:
                    dbg(f"Nepodařilo se získat product ID pro {product_link}, přeskočeno")
                    continue

                # Zpracovat nový produkt
                new_product_url = build_product_url(product_link, product_id, False)
                product_data = scrape_product_page(new_product_url, False)

                if product_data:
                    row = [
                        product_data["product_name"],
                        product_data["price"],
                        product_data["delivery_time"],
                        product_data["supplier_number"],
                        product_data["images"],
                        product_data["description"],
                        product_data["category_path"]
                    ]

                    if file_format == "excel":
                        save_to_excel(row, output_file)
                    else:
                        save_to_csv(row, output_file)

                    product_count += 1
                    dbg(f"Produkt uložen: {product_data['product_name']}")

                # Zpracovat repasovaný produkt
                refurbished_product_url = build_product_url(product_link, product_id, True)
                product_data = scrape_product_page(refurbished_product_url, True)

                if product_data:
                    row = [
                        product_data["product_name"],
                        product_data["price"],
                        product_data["delivery_time"],
                        product_data["supplier_number"],
                        product_data["images"],
                        product_data["description"],
                        product_data["category_path"]
                    ]

                    if file_format == "excel":
                        save_to_excel(row, output_file)
                    else:
                        save_to_csv(row, output_file)

                    product_count += 1
                    dbg(f"Repasovaný produkt uložen: {product_data['product_name']}")

                # Uložit progress
                if progress_data:
                    progress_data["page"] = page
                    progress_data["product_count"] = product_count
                    with open("progress.json", "w", encoding="utf-8") as f:
                        json.dump(progress_data, f, ensure_ascii=False, indent=2)

                # Počkat mezi požadavky
                time.sleep(delay)

            page += 1

        except Exception as e:
            dbg(f"Chyba při zpracování stránky {page}: {e}")
            break

    return product_count


def run_it_planet_scraper():
    """Hlavní funkce pro spuštění scrapingu."""
    # Zeptat se na výstupní formát
    print("Vyber výstupní formát:")
    print("  1) Excel (.xlsx)")
    print("  2) CSV (.csv)")
    format_choice = input("Zadej 1 nebo 2: ").strip()

    if format_choice == "1":
        file_format = "excel"
        default_file = "it-planetData.xlsx"
    else:
        file_format = "csv"
        default_file = "it-planetData.csv"

    output_file = input(f"Zadej název výstupního souboru (enter pro {default_file}): ").strip() or default_file
    output_path = _resolve_output_path(output_file)

    # Zkontrolovat příponu souboru
    if file_format == "excel" and not output_file.endswith('.xlsx'):
        output_file += '.xlsx'
    elif file_format == "csv" and not output_file.endswith('.csv'):
        output_file += '.csv'

    # Získat sekce a podsekce
    dbg("Získávám sekce a podsekce...")
    sections = get_sections_and_subsections()

    if not sections:
        dbg("Nepodařilo se získat sekce. Ukončuji.")
        return

    # Zobrazit sekce
    print("\nDostupné sekce:")
    section_names = list(sections.keys())
    for i, section_name in enumerate(section_names, 1):
        print(f"  {i}. {section_name}")

    section_choice = input("\nVyber sekci (číslo nebo název, nebo 'vše' pro všechny): ").strip()

    selected_sections = []
    if section_choice.lower() in ("vse", "vše", "all", "everything"):
        selected_sections = section_names
    elif section_choice.isdigit():
        idx = int(section_choice)
        if 1 <= idx <= len(section_names):
            selected_sections = [section_names[idx - 1]]
        else:
            print("Neplatný index sekce.")
            return
    else:
        # Hledat podle názvu
        matching_sections = [name for name in section_names if section_choice.lower() in name.lower()]
        if matching_sections:
            selected_sections = [matching_sections[0]]
        else:
            print("Sekce nenalezena.")
            return

    # Pro každou vybranou sekci vybrat podsekce
    all_subsections = {}
    for section_name in selected_sections:
        section = sections[section_name]
        subsections = section["subsections"]

        if not subsections:
            # Sekce nemá podsekce, použijeme přímo URL sekce
            all_subsections[section_name] = section["url"]
        else:
            print(f"\nPodsekce pro {section_name}:")
            subsection_names = list(subsections.keys())
            for i, subsection_name in enumerate(subsection_names, 1):
                print(f"  {i}. {subsection_name}")

            subsection_choice = input(
                f"\nVyber podsekci pro {section_name} (číslo nebo název, nebo 'vše' pro všechny): ").strip()

            if subsection_choice.lower() in ("vse", "vše", "all", "everything"):
                for name, url in subsections.items():
                    all_subsections[f"{section_name} > {name}"] = url
            elif subsection_choice.isdigit():
                idx = int(subsection_choice)
                if 1 <= idx <= len(subsection_names):
                    name = subsection_names[idx - 1]
                    all_subsections[f"{section_name} > {name}"] = subsections[name]
                else:
                    print("Neplatný index podsekce.")
                    return
            else:
                # Hledat podle názvu
                matching_subsections = [name for name in subsection_names if subsection_choice.lower() in name.lower()]
                if matching_subsections:
                    name = matching_subsections[0]
                    all_subsections[f"{section_name} > {name}"] = subsections[name]
                else:
                    print("Podsekce nenalezena.")
                    return

    # Zeptat se na zpoždění mezi požadavky
    delay_input = input("Zadej zpoždění mezi požadavky v sekundách (enter pro 1.0): ").strip()
    delay = float(delay_input) if delay_input else 1.0

    # Zeptat se na pokračování
    progress_data = None
    try:
        with open("progress.json", "r", encoding="utf-8") as f:
            progress_data = json.load(f)
            print(f"Nalezen uložený progress: {progress_data}")
    except FileNotFoundError:
        pass

    if progress_data:
        resume = input("Chceš pokračovat z uloženého progressu? (ano/ne): ").strip().lower()
        if resume != "ano":
            progress_data = None

    # Scrapovat vybrané podsekce
    total_products = 0
    subsection_items = list(all_subsections.items())

    for i, (subsection_name, subsection_url) in enumerate(subsection_items):
        dbg(f"Zpracovávám podsekci {i + 1}/{len(subsection_items)}: {subsection_name}")

        if progress_data and progress_data.get("subsection_name") != subsection_name:
            continue

        if progress_data:
            # Pokračujeme z uloženého progressu
            product_count = scrape_subsection(
                subsection_url, output_path, file_format, delay, progress_data
            )
        else:
            # Začínáme novou podsekci
            progress_data = {
                "subsection_name": subsection_name,
                "subsection_url": subsection_url,
                "page": 1,
                "product_count": 0
            }
            product_count = scrape_subsection(
                subsection_url, output_path, file_format, delay, progress_data
            )

        total_products += product_count
        dbg(f"Podsekce {subsection_name} dokončena. Celkem produktů: {product_count}")

        # Resetovat progress po dokončení podsekce
        progress_data = None
        try:
            Path("progress.json").unlink()
        except:
            pass

    dbg(f"Scraping dokončen. Celkem uloženo produktů: {total_products}")


if __name__ == "__main__":
    run_it_planet_scraper()