import re
import time
from urllib.parse import urlparse, urlunparse

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, StaleElementReferenceException
)

import openpyxl
from openpyxl import Workbook


def dbg(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def chrome_driver(headless=True):
    opts = webdriver.ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--window-size=1400,1600")
    return webdriver.Chrome(options=opts)


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


if __name__ == "__main__":
    scrape_product_data(
        'https://it-market.com/en/communication/wireless/access-points/ubiquiti/uap-ac-m-pro',
        excel_file='product_data.xlsx',
        headless=True
    )
