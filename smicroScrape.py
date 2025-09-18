import requests
from bs4 import BeautifulSoup
import csv
import os
import re
from urllib.parse import urljoin
import pandas as pd


def smicro_scrape_product_page():
    """Hlavní funkce pro scrapování produktů ze seznamu stránek"""
    # Požádat uživatele o URL
    base_url = input("Zadejte URL stránky produktů z smicro.cz: ")

    # Inicializace CSV souboru a získání posledního indexu
    csv_filename = 'smicro_products.csv'
    index_counter = init_csv_and_get_last_index(csv_filename)

    page = 0

    while True:
        # Sestavení URL s parametrem stránky
        if page == 0:
            url = base_url
        else:
            url = f"{base_url}&page={page}"

        print(f"Scrapuji stránku: {page + 1}")

        # Získání HTML obsahu
        response = requests.get(url)
        if response.status_code != 200:
            print(f"Chyba při načítání stránky: {response.status_code}")
            break

        soup = BeautifulSoup(response.text, 'html.parser')

        # Najít kontejner s produkty
        product_container = soup.find('div', {'id': 'productAjaxPagerContainer'})
        if not product_container:
            print("Nebyl nalezen kontejner produktů")
            break

        # Získat odkazy na produkty
        product_links = []
        for item in product_container.find_all('div', class_='item'):
            link_tag = item.find('h3').find('a')
            if link_tag and link_tag.get('href'):
                full_url = urljoin(base_url, link_tag['href'])
                product_links.append(full_url)

        # Pokud nejsou žádné produkty, ukončit
        if not product_links:
            print("Žádné produkty na stránce")
            break

        # Scrapovat detaily každého produktu
        for product_url in product_links:
            print(f"Scrapuji produkt: {product_url}")
            product_data = smicro_scrape_product_details(product_url)
            if product_data:
                save_to_csv(csv_filename, index_counter, product_data)
                index_counter += 1

        # Kontrola další stránky
        next_page_tag = soup.find('a', {'id': f'pageNonactive-{page + 1}'})
        if next_page_tag:
            page += 1
        else:
            print("Neexistuje další stránka")
            break

    print(f"Scrapování dokončeno. Data uložena v {csv_filename}")

    # Konverze CSV do Excelu
    convert_csv_to_excel(csv_filename)
    print(f"Excel soubor byl vytvořen: {csv_filename.replace('.csv', '.xlsx')}")

def convert_csv_to_excel(csv_filename):
    """Konvertuje CSV soubor do Excel formátu (.xlsx) s formátováním cen jako Kč"""
    excel_filename = csv_filename.replace('.csv', '.xlsx')

    # Načtení CSV souboru
    df = pd.read_csv(csv_filename, delimiter=';', encoding='utf-8')

    # Přejmenování sloupců podle požadavků
    df.columns = [
        'index',
        'název produktu',
        'part number',
        'Dostupnost u nás',
        'Dostupnost u dodavatele',
        'Cena bez DPH',
        'Cena s DPH',
        'Specifikace'
    ]

    # Uložení do Excelu s formátováním
    with pd.ExcelWriter(excel_filename, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Produkty')

        # Získání worksheetu
        worksheet = writer.sheets['Produkty']

        # Formátování sloupců s cenami jako české koruny
        money_format = '#,##0" Kč"'

        # Formát pro sloupec F (Cena bez DPH)
        for cell in worksheet['F']:
            cell.number_format = money_format

        # Formát pro sloupec G (Cena s DPH)
        for cell in worksheet['G']:
            cell.number_format = money_format

        # Automatické nastavení šířky sloupců podle obsahu
        for column in worksheet.columns:
            max_length = 0
            column_letter = column[0].column_letter

            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass

            # Přidání malé rezervy
            adjusted_width = (max_length + 2) * 1.2
            worksheet.column_dimensions[column_letter].width = adjusted_width

def smicro_scrape_product_details(url):
    """Funkce pro scrapování detailů jednotlivého produktu"""
    response = requests.get(url)
    if response.status_code != 200:
        print(f"Chyba při načítání produktu: {response.status_code}")
        return None

    soup = BeautifulSoup(response.text, 'html.parser')
    product_data = {}

    # Název produktu
    name_tag = soup.find('h1')
    product_data['name'] = name_tag.text.strip() if name_tag else "Neznámý název"

    # Part number a dostupnost
    table_data = soup.find('table', class_='tabData')
    if table_data:
        rows = table_data.find_all('tr')
        for row in rows:
            th = row.find('th')
            td = row.find('td')
            if th and td:
                header = th.text.strip()
                value = td.text.strip()

                if 'Part number' in header:
                    product_data['part_number'] = value
                elif 'Dostupnost u nás' in header:
                    # Extrahujeme pouze informaci o počtu kusů (např. "Skladem 5 ks")
                    stock_match = re.search(r'Skladem (\d+ ks)', value)
                    if stock_match:
                        product_data['availability_local'] = stock_match.group(0)
                    else:
                        product_data['availability_local'] = value.split('\n')[0].strip()
                elif 'Dostupnost u dodavatele' in header:
                    # Extrahujeme pouze informaci o počtu kusů (např. "Skladem 5 ks")
                    stock_match = re.search(r'Skladem (\d+ ks)', value)
                    if stock_match:
                        product_data['availability_supplier'] = stock_match.group(0)
                    else:
                        product_data['availability_supplier'] = value.split('\n')[0].strip()

    # Ceny
    price_div = soup.find('div', class_='detPrice')
    if price_div:
        price_without_vat = price_div.find('div', class_='cena')
        price_with_vat = price_div.find('div', class_='cenaDph')

        if price_without_vat:
            price_text = price_without_vat.text.replace('bez DPH', '').strip()
            numbers = re.findall(r'[\d\s]+', price_text)
            if numbers:
                clean_number = numbers[0].replace(' ', '')
                product_data['price_without_vat'] = clean_number

        if price_with_vat:
            price_text = price_with_vat.text.replace('s DPH', '').strip()
            numbers = re.findall(r'[\d\s]+', price_text)
            if numbers:
                clean_number = numbers[0].replace(' ', '')
                product_data['price_with_vat'] = clean_number

    # Specifikace
    spec_table = soup.find('table', class_='tabParam')
    specifications = []
    if spec_table:
        rows = spec_table.find_all('tr')
        for row in rows:
            th = row.find('th')
            td = row.find('td')
            if th and td:
                spec_name = th.text.strip()
                spec_value = td.text.strip()
                specifications.append(f"{spec_name}: {spec_value}")

    product_data['specifications'] = '; '.join(specifications)

    return product_data

def init_csv_and_get_last_index(filename):
    """Inicializace CSV souboru s hlavičkou a získání posledního indexu"""
    headers = [
        'index', 'název produktu', 'part number',
        'Dostupnost u nás', 'Dostupnost u dodavatele',
        'Cena bez DPH', 'Cena s DPH', 'Specifikace'
    ]

    if not os.path.exists(filename):
        with open(filename, 'w', newline='', encoding='utf-8') as file:
            writer = csv.writer(file, delimiter=';')
            writer.writerow(headers)
        return 1

    with open(filename, 'r', newline='', encoding='utf-8') as file:
        reader = csv.reader(file, delimiter=';')
        rows = list(reader)

        if len(rows) <= 1:
            return 1

        last_row = rows[-1]
        try:
            last_index = int(last_row[0])
            return last_index + 1
        except (IndexError, ValueError):
            return 1

def save_to_csv(filename, index, product_data):
    """Uložení dat produktu do CSV s kontrolou duplicit"""
    # Načtení existujících dat
    existing_data = set()
    if os.path.exists(filename):
        with open(filename, 'r', newline='', encoding='utf-8') as file:
            reader = csv.reader(file, delimiter=';')
            next(reader)  # Přeskočit hlavičku
            for row in reader:
                if len(row) > 2:  # Kontrola, zda řádek obsahuje part_number
                    existing_data.add(row[2])  # part_number je ve 3. sloupci

    # Pokud part_number už existuje, přeskočit zápis
    if product_data.get('part_number') in existing_data:
        print(f"Přeskakuji duplicitní produkt: {product_data.get('part_number')}")
        return

    # Zápis nového záznamu
    with open(filename, 'a', newline='', encoding='utf-8') as file:
        writer = csv.writer(file, delimiter=';')
        writer.writerow([
            index,
            product_data.get('name', ''),
            product_data.get('part_number', ''),
            product_data.get('availability_local', ''),
            product_data.get('availability_supplier', ''),
            product_data.get('price_without_vat', ''),
            product_data.get('price_with_vat', ''),
            product_data.get('specifications', '')
        ])


if __name__ == "__main__":
    smicro_scrape_product_page()
    csv_filename = 'smicro_products.csv'
    convert_csv_to_excel(csv_filename)