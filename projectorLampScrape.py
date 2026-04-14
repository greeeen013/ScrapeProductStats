import csv
import json
import os
import sys

import requests
from bs4 import BeautifulSoup
import time
from urllib.parse import urljoin
from pathlib import Path

WARP_PROXY = {"http": "socks5h://127.0.0.1:40000", "https": "socks5h://127.0.0.1:40000"}
SCRIPT_DIR = Path(__file__).resolve().parent
PROGRESS_FILE = SCRIPT_DIR / "projectorLampProgress.json"


# === SPRÁVA PROGRESSU ===
def save_progress(brand, done_urls):
    try:
        with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
            json.dump({"brand": brand, "done_urls": list(done_urls)}, f, ensure_ascii=False)
    except Exception:
        pass


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


def ziskej_vyrobce():
    """Získá seznam všech výrobců z hlavní stránky"""
    url = "https://www.myprojectorlamps.eu"
    response = requests.get(url, proxies=WARP_PROXY)
    soup = BeautifulSoup(response.content, 'html.parser')

    select = soup.find('select', {'id': 'brands-select'})
    options = select.find_all('option')[1:]  # První option je "Brand", přeskočíme

    vyrobci = []
    for option in options:
        vyrobci.append({
            'value': option['value'],
            'nazev': option.text.strip()
        })
    return vyrobci


def ziskej_produkty_vyrobce(vyrobce_nazev):
    """Získá všechny produkty konkrétního výrobce"""
    url = f"https://www.myprojectorlamps.eu/projectors/{vyrobce_nazev}"
    response = requests.get(url, proxies=WARP_PROXY)
    soup = BeautifulSoup(response.content, 'html.parser')

    select = soup.find('select', {'id': 'lamps-select'})
    if not select:
        return []

    produkty = []
    for option in select.find_all('option')[1:]:  # Přeskočíme první option
        if option['value'] != '-':
            produkty.append(option['value'])

    return produkty


def zpracuj_produkt(url, soubor):
    """Zpracuje detail produktu a zapíše data do CSV"""
    response = requests.get(url, proxies=WARP_PROXY)
    soup = BeautifulSoup(response.content, 'html.parser')

    tech_info = soup.find('div', class_='product-table-tech-info')
    if not tech_info:
        return False

    part_number = None
    brand = None
    for row in tech_info.find_all('tr'):
        cells = row.find_all('td')
        if len(cells) >= 2:
            if cells[0].text.strip() == 'Lamp Part Number':
                part_number = cells[1].text.strip()
            elif cells[0].text.strip() == 'Brand':
                brand = cells[1].text.strip()

    if part_number == 'See "Alternative Lamp ID\'s"':
        return False

    projektory_section = soup.find('div', class_='suitable-projectors-minimalistic')
    kompatibilni = []
    if projektory_section:
        for li in projektory_section.find_all('li'):
            kompatibilni.append(li.text.strip())

    with open(soubor, 'a', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f, delimiter=';', quoting=csv.QUOTE_ALL)
        writer.writerow([brand, part_number, '; '.join(kompatibilni)])

    return True


def nacti_nebo_vytvor_csv(soubor):
    """Vytvoří CSV soubor s hlavičkou, pokud ještě neexistuje"""
    if not os.path.exists(soubor):
        with open(soubor, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f, delimiter=';', quoting=csv.QUOTE_ALL)
            writer.writerow(['Brand', 'Lamp Part Number', 'Suitable Projectors'])


def run_test():
    print("=== MyProjectorLamps.eu Test ===")
    try:
        vyrobci = ziskej_vyrobce()
        if not vyrobci:
            print("TEST ERROR: Nepodařilo se načíst výrobce")
            sys.exit(1)
        prvni = vyrobci[0]
        produkty = ziskej_produkty_vyrobce(prvni['nazev'])
        print(f"TEST OK: {len(vyrobci)} výrobců načteno, '{prvni['nazev']}': {len(produkty)} produktů")
        sys.exit(0)
    except Exception as e:
        print(f"TEST ERROR: {e}")
        sys.exit(1)


def main():
    if '--test' in sys.argv:
        run_test()
        return

    soubor = 'vysledky.csv'
    nacti_nebo_vytvor_csv(soubor)

    vyrobci = ziskej_vyrobce()

    # Výběr výrobce
    print("Dostupné značky:")
    for i, vyrobce in enumerate(vyrobci, 1):
        print(f"{i}. {vyrobce['nazev']}")

    volba = input("Zadejte číslo výrobce, název výrobce nebo 'vše' pro všechny: ")

    if volba.lower() == 'vše':
        vybrani_vyrobci = vyrobci
    else:
        najity_vyrobce = None
        for vyrobce in vyrobci:
            if vyrobce['nazev'].lower() == volba.lower():
                najity_vyrobce = vyrobce
                break
        if not najity_vyrobce:
            try:
                index = int(volba) - 1
                if 0 <= index < len(vyrobci):
                    najity_vyrobce = vyrobci[index]
            except ValueError:
                pass
        if najity_vyrobce:
            vybrani_vyrobci = [najity_vyrobce]
            print(f"Vybraný výrobce: {najity_vyrobce['nazev']}")
        else:
            print("Neznámý výrobce!")
            return

    # Načtení progressu
    start_brand = None
    done_urls = set()
    progress = load_progress()
    if progress:
        print(f"\nNalezen uložený postup: výrobce '{progress['brand']}', {len(progress.get('done_urls', []))} produktů hotovo")
        ans = input("Pokračovat od posledního místa? (ano/ne): ").strip().lower()
        if ans == 'ano':
            start_brand = progress['brand']
            done_urls = set(progress.get('done_urls', []))
        else:
            clear_progress()

    skip_to_brand = start_brand is not None

    # Zpracování vybraných výrobců
    for vyrobce in vybrani_vyrobci:
        if skip_to_brand:
            if vyrobce['nazev'] != start_brand:
                print(f"  (přeskakuji: {vyrobce['nazev']})")
                continue
            else:
                skip_to_brand = False
        else:
            done_urls = set()  # nový výrobce = žádné hotové URL

        print(f"Zpracovávám {vyrobce['nazev']}...")
        produkty = ziskej_produkty_vyrobce(vyrobce['nazev'])
        print(f"Našel jsem {len(produkty)} produktů")

        skipped = sum(1 for u in produkty if u in done_urls)
        if skipped:
            print(f"  ({skipped} produktů přeskočeno – již hotovo)")

        for produkt_url in produkty:
            if produkt_url in done_urls:
                continue
            print(f"  Zpracovávám {produkt_url}")
            uspech = zpracuj_produkt(produkt_url, soubor)
            if uspech:
                done_urls.add(produkt_url)
                save_progress(vyrobce['nazev'], done_urls)
                time.sleep(1)

    clear_progress()
    print(f"Data uložena do {soubor}")


if __name__ == "__main__":
    main()