import requests

TARGET = "https://it-market.com/en"

def get_public_ip_and_geo():
    """Zjistí veřejnou IP a geolokaci přes ip-api.com (zdarma, bez API klíče)."""
    r = requests.get("http://ip-api.com/json/?fields=query,country,regionName,city,isp,org,as", timeout=10)
    return r.json()

def get_cloudflare_trace():
    """Cloudflare trace — ukáže IP + jestli je WARP zapnutý."""
    r = requests.get("https://www.cloudflare.com/cdn-cgi/trace", timeout=10)
    data = {}
    for line in r.text.strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    return data

def ping_target(url):
    """Pošle GET request na cílovou stránku a vrátí status."""
    r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
    return r.status_code, r.url

print("=" * 55)
print("  IP & lokace z pohledu webových stránek")
print("=" * 55)

# 1. Co vidí webové stránky — IP + geolokace
print("\n[1] Veřejná IP a přibližná lokace (ip-api.com):")
try:
    geo = get_public_ip_and_geo()
    print(f"    IP adresa : {geo.get('query')}")
    print(f"    Stát      : {geo.get('country')}")
    print(f"    Region    : {geo.get('regionName')}")
    print(f"    Město     : {geo.get('city')}")
    print(f"    ISP       : {geo.get('isp')}")
    print(f"    Org       : {geo.get('org')}")
    print(f"    AS        : {geo.get('as')}")
except Exception as e:
    print(f"    Chyba: {e}")

# 2. Cloudflare trace — WARP status
print("\n[2] Cloudflare trace (ověření WARP):")
try:
    cf = get_cloudflare_trace()
    print(f"    IP adresa : {cf.get('ip')}")
    print(f"    Lokace    : {cf.get('loc')}")
    print(f"    WARP      : {cf.get('warp')}  ← 'on' = WARP aktivní, 'off' = bez WARP")
    print(f"    Gateway   : {cf.get('gateway')}")
except Exception as e:
    print(f"    Chyba: {e}")

# 3. Ping cílové stránky
print(f"\n[3] Ping cílové stránky: {TARGET}")
try:
    status, final_url = ping_target(TARGET)
    print(f"    HTTP status : {status}")
    print(f"    Finální URL : {final_url}")
    print(f"    Stránka odpověděla — to je IP pod kterou tě vidí it-market.com")
except Exception as e:
    print(f"    Chyba: {e}")

print("\n" + "=" * 55)
print("  Tip: spusť jednou bez WARP, jednou s WARP")
print("  a porovnej IP adresy a lokace.")
print("=" * 55)
