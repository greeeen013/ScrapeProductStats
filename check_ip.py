import socket
import subprocess
import requests

WARP_PROXY = {"http": "socks5h://127.0.0.1:40000", "https": "socks5h://127.0.0.1:40000"}

TARGET = "https://it-market.com/en"
FALLBACK_DNS = "8.8.8.8"

# Patch DNS — pokud Tailscale MagicDNS selže na externí doméně,
# zkusí fallback přes 8.8.8.8 (Google DNS)
_orig_getaddrinfo = socket.getaddrinfo

def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    try:
        return _orig_getaddrinfo(host, port, family, type, proto, flags)
    except socket.gaierror:
        try:
            result = subprocess.run(
                ["nslookup", host, FALLBACK_DNS],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if "Address:" in line and FALLBACK_DNS not in line:
                    ip = line.split("Address:")[-1].strip()
                    if ip and ":" not in ip:  # preferuj IPv4
                        return _orig_getaddrinfo(ip, port, family, type, proto, flags)
        except Exception:
            pass
        raise

socket.getaddrinfo = _patched_getaddrinfo

def get_public_ip_and_geo():
    """Zjistí veřejnou IP a geolokaci přes ip-api.com (zdarma, bez API klíče)."""
    r = requests.get("http://ip-api.com/json/?fields=query,country,regionName,city,isp,org,as", timeout=10, proxies=WARP_PROXY)
    return r.json()

def get_cloudflare_trace():
    """Cloudflare trace — ukáže IP + jestli je WARP zapnutý."""
    r = requests.get("https://www.cloudflare.com/cdn-cgi/trace", timeout=10, proxies=WARP_PROXY)
    data = {}
    for line in r.text.strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    return data

def ping_target(url):
    """Pošle GET request na cílovou stránku a vrátí status."""
    r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"}, proxies=WARP_PROXY)
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
