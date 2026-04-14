# ScrapeProductStats

Kolekce scraperů pro různé IT/hardware e-shopy. Výstup je vždy **CSV** (oddělovač `;`, QUOTE_ALL, encoding UTF-8 BOM) nebo XLSX (starší skripty). Správa a spouštění přes webový frontend – **Scraper Manager**.

---

## Struktura projektu

```
ScrapeProductStats/
│
├── scraper-manager/          # Web UI + FastAPI backend
│   ├── server.py             # REST API + WebSocket log streaming
│   ├── static/
│   │   └── index.html        # Single-page frontend
│   └── requirements.txt      # fastapi, uvicorn, httpx[socks]
│
├── smicroScrapePlayWright.py      # smicro.cz  (Playwright, async)
├── it-marketScrapePlayWright.py   # it-market.com (Playwright, async)
├── it-planetScrapePlayWright.py   # it-planet.com (Playwright, async)
├── projectorLampScrape.py         # myprojectorlamps.eu (requests + BS4, sync)
│
├── smicro_products.csv        # výstup smicro
├── it-market.csv              # výstup it-market
├── it-planet_data.csv         # výstup it-planet
├── vysledky.csv               # výstup projector lamps
│
├── *Progress.json / *LastProduct.json   # soubory průběhu (resume)
├── html_dumps/                # HTML zálohy při Cloudflare bloku
├── check_ip.py                # ověření proxy/WARP před spuštěním
└── requirements.txt
```

---

## Instalace

```bash
pip install -r requirements.txt
playwright install chromium
```

Pro Scraper Manager:
```bash
cd scraper-manager
pip install -r requirements.txt   # fastapi, uvicorn, httpx[socks]
```

---

## Proxy / WARP

Všechny scrapery používají lokální SOCKS5 proxy na `127.0.0.1:40000` (Cloudflare WARP nebo Tailscale). Před spuštěním ověř dostupnost:

```bash
python check_ip.py
```

Pokud proxy není dostupná, Playwright scrapery se automaticky připojí přímo (bez proxy).

---

## Spuštění scraperů ručně (CLI)

```bash
python smicroScrapePlayWright.py
python it-marketScrapePlayWright.py
python it-planetScrapePlayWright.py
python projectorLampScrape.py
```

Každý skript je interaktivní – ptá se na kategorii/sekci, počet workerů, headless/headful atd.

### Test mode

Každý skript podporuje `--test` flag, který ověří konektivitu a dostupnost webu bez plného scrapování:

```bash
python smicroScrapePlayWright.py --test
# TEST OK: 12 kategorií načteno, 'RAM Paměti': 24 produktů na straně 1
```

Vrátí exit code `0` (OK) nebo `1` (ERROR).

---

## Scraper Manager (webové UI)

```bash
cd scraper-manager
python server.py
# Otevři http://localhost:8000
```

### Jak funguje spuštění scraperu z frontendu

1. **Uživatel klikne "▶ Spustit"** na kartě scraperu v levém panelu.
2. Otevře se **modální dialog** s formulářem – pole odpovídají vstupu, který by jinak skript četl přes `input()` (počet workerů, sekce, headless atd.).
3. Po potvrzení frontend pošle `POST /api/runs` s `scraper_id` a vyplněnými hodnotami.
4. Server spustí skript jako **subprocess** (`asyncio.create_subprocess_exec`) a nakrmí ho hodnotami formuláře přes **stdin** (jeden řádek = jedna odpověď na `input()`). Skript se spustí přesně tak, jako kdyby ho uživatel spustil ručně v terminálu.
5. Výstup procesu (stdout + stderr) se **streamuje přes WebSocket** `/ws/runs/{id}` do logu v pravém panelu. Ukládá se do fronty `deque(maxlen=500)` v paměti serveru.
6. Run je viditelný v hlavním panelu se stavem `running / completed / failed / stopped`.
7. Po dokončení lze **stáhnout CSV** přes tlačítko "⬇ Stáhnout" nebo přímo z karty scraperu.

### Test tlačítko 🧪

Spustí skript s `--test` flaggem jako subprocess, počká max 60 s a zobrazí výsledek v modálním okně:
- **TEST OK** – scraper se připojil a našel produkty
- **TEST ERROR** – problém s připojením, proxy, nebo strukturou stránky

### Proxy status

Čip **WARP** v hlavičce zobrazuje stav proxy. Kliknutím se otevře boční panel se stavem (IP, WARP, ISP) načteným přes Cloudflare trace a ip-api přes proxy. Výsledek se cachuje 60 s.

---

## Resume – pokračování po přerušení

Každý scraper ukládá po každém úspěšně scrapnutém produktu soubor průběhu (`*Progress.json`):

```json
{
  "section": "Switches",
  "page": 4,
  "done_urls": [
    "https://it-market.com/en/switches/cisco/xyz",
    "https://it-market.com/en/switches/cisco/abc"
  ]
}
```

Při příštím spuštění skript nalezne soubor a zeptá se:
> `Pokračovat od posledního místa? (ano/ne):`

Pokud zvolíš `ano`:
- Přeskočí sekce/kategorie před tou, kde se přestalo
- Na stránce přerušení přeskočí URL, které jsou v `done_urls` (již scrapnuté produkty)
- Pokračuje od prvního nescrapnutého produktu

Po úspěšném dokončení se soubor průběhu **automaticky smaže**.

---

## Formát výstupního CSV

Všechny scrapery produkují CSV se stejnými vlastnostmi:

| Vlastnost   | Hodnota          |
|-------------|------------------|
| Oddělovač   | `;`              |
| Uvozování   | všechna pole     |
| Encoding    | UTF-8 BOM (utf-8-sig) |
| Hlavička    | 1. řádek         |

Konkrétní sloupce se liší scraper od scraperu (každý web má jiná data), ale formát souboru je identický – lze otevřít přímo v Excelu.

---

## Architektura scraperů

### Playwright scrapery (preferované)

- **Async Python** s `asyncio.Semaphore` pro paralelní okna
- Stealth JS (`navigator.webdriver = undefined`, falešné pluginy atd.)
- Detekce Cloudflare challenge → dump HTML do `html_dumps/`
- Proxy: `browser.launch(proxy={"server": "socks5://127.0.0.1:40000"})`
- Varianty produktů: kliknutí na radio button → čekání na AJAX → extrakce dat

### projectorLampScrape.py (requests + BeautifulSoup)

- Synchronní, jednoduchý
- `requests` s SOCKS5 proxy přes `socks5h://`
- Výstup CSV (stejný formát jako Playwright scrapery)

---

## Přidání nového scraperu do Manageru

1. Napiš skript (nebo zkopíruj existující jako základ)
2. Přidej záznam do `SCRAPERS` dict v `scraper-manager/server.py`:
   ```python
   "muj-scraper": {
       "id": "muj-scraper",
       "name": "Můj E-shop",
       "description": "...",
       "script": str(SCRAPERS_DIR / "mojeScrape.py"),
       "output_file": str(SCRAPERS_DIR / "moje_data.csv"),
       "progress_file": str(SCRAPERS_DIR / "mojeProgress.json"),
       "inputs": [
           {"id": "workers", "label": "Počet workerů", "default": "3", "type": "number"},
           {"id": "section", "label": "Sekce",         "default": "vse", "type": "text"},
       ],
   }
   ```
3. Pole `inputs` definují formulář ve frontendu – pořadí odpovídá pořadí `input()` volání v skriptu
4. Přidej `--test` mód do skriptu pro fungující Test tlačítko
