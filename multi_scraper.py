import re
import json
import time
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Session & Headers
# ---------------------------------------------------------------------------

_SESSION = requests.Session()

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "DNT": "1",
}

_AMAZON_HEADERS = {**_BASE_HEADERS, "Referer": "https://www.amazon.com.br/", "Cache-Control": "no-cache", "Pragma": "no-cache"}
_KABUM_HEADERS  = {**_BASE_HEADERS, "Referer": "https://www.kabum.com.br/"}
_MAGALU_HEADERS = {**_BASE_HEADERS, "Referer": "https://www.magazineluiza.com.br/"}

# ---------------------------------------------------------------------------
# Price validation constants
# ---------------------------------------------------------------------------

# Minimum plausible total price for a Nintendo Switch 2 console.
# Any extracted value below this is treated as an installment fragment or accessory.
MIN_VALID_PRICE = 1500.0

# Text patterns that indicate an instalment context — any element whose
# nearby text contains these is skipped.
_INSTALLMENT_KEYWORDS = re.compile(
    r"\b(em\s+até|\d+x|\bmensal\b|por\s+m[eê]s|parcela|prestação|vezes)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clean_price(text):
    """
    Convert Brazilian price string to float.
    'R$ 3.846,55' -> 3846.55  |  '3800' -> 3800.0
    Returns None if value is below MIN_VALID_PRICE or unparseable.
    """
    if not text:
        return None
    cleaned = re.sub(r"[^\d,]", "", text.strip())
    if not cleaned:
        return None
    if "," in cleaned:
        parts = cleaned.rsplit(",", 1)
        cleaned = parts[0].replace(",", "") + "." + parts[1]
    try:
        val = float(cleaned)
        return val if val >= MIN_VALID_PRICE else None
    except ValueError:
        return None


def _is_installment_context(element):
    """
    Returns True if the element or its parent text contains installment keywords
    (e.g. '12x', 'em até', 'mensal', 'por mês').
    """
    # Check the element itself and up to 2 ancestor levels
    node = element
    for _ in range(3):
        if node is None:
            break
        text = node.get_text(" ", strip=True)
        if _INSTALLMENT_KEYWORDS.search(text):
            return True
        node = node.parent
    return False


def _detect_platform(url):
    host = urlparse(url).hostname or ""
    if "amazon" in host:
        return "amazon"
    if "kabum" in host:
        return "kabum"
    if "magazineluiza" in host or "magalu" in host:
        return "magalu"
    return "generic"


def _fetch(url, headers, timeout=25):
    resp = _SESSION.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    return resp


def _parse_jsonld(soup):
    """Extract lowest valid price from JSON-LD structured data."""
    best = None
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            t = item.get("@type", "")
            price = None
            if t == "Product":
                offers = item.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                price = offers.get("price") or offers.get("lowPrice")
            elif t in ("Offer", "AggregateOffer"):
                price = item.get("price") or item.get("lowPrice")
            if price is not None:
                try:
                    val = float(str(price).replace(",", "."))
                    if val >= MIN_VALID_PRICE and (best is None or val < best):
                        best = val
                except ValueError:
                    pass
    return best


# ---------------------------------------------------------------------------
# Platform-specific parsers
# ---------------------------------------------------------------------------

def _scrape_amazon(soup):
    """
    Amazon parser — 3-layer fallback:
      1. AOD offer list (#aod-offer-list)
      2. Buy-box price selectors (7 candidates)
      3. JSON-LD (injected by caller)
    Skips any element whose context contains instalment keywords.
    """
    result = {"cash_price": None, "seller_name": "Amazon", "is_fba": False}

    # 1. AOD offer list
    aod_list = soup.select_one("#aod-offer-list")
    if aod_list:
        best_price, best_seller, best_fba = None, "Amazon", False
        for offer in aod_list.select("#aod-offer"):
            price_el = offer.select_one(".a-offscreen")
            if price_el and _is_installment_context(price_el):
                continue
            price = clean_price(price_el.get_text()) if price_el else None
            if not price:
                whole = offer.select_one(".a-price-whole")
                frac  = offer.select_one(".a-price-fraction")
                if whole and not _is_installment_context(whole):
                    raw = whole.get_text(strip=True).rstrip(",.")
                    if frac:
                        raw += "," + frac.get_text(strip=True)
                    price = clean_price(raw)
            if not price:
                continue
            seller_el = (
                offer.select_one("#aod-offer-soldBy .a-size-small") or
                offer.select_one("#aod-offer-soldBy a") or
                offer.select_one("#aod-offer-soldBy span")
            )
            seller = seller_el.get_text(strip=True) if seller_el else "Amazon"
            fba = (
                "Enviado pela Amazon" in offer.get_text() or
                "Fulfilled by Amazon" in offer.get_text() or
                seller.lower() == "amazon"
            )
            if best_price is None or price < best_price:
                best_price, best_seller, best_fba = price, seller, fba

        if best_price:
            result.update(cash_price=best_price, seller_name=best_seller, is_fba=best_fba)
            return result

    # 2. Buy-box selectors
    buybox_selectors = [
        "#corePrice_feature_div .a-offscreen",
        "#corePrice_feature_div .a-price .a-offscreen",
        ".a-price .a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "#price_inside_buybox",
        "#apex_offerDisplay_desktop .a-price .a-offscreen",
    ]
    for sel in buybox_selectors:
        el = soup.select_one(sel)
        if el and not _is_installment_context(el):
            price = clean_price(el.get_text())
            if price:
                result["cash_price"] = price
                break

    seller_el = (
        soup.select_one("#sellerProfileTriggerId") or
        soup.select_one("#merchant-info a") or
        soup.select_one("#merchant-info")
    )
    if seller_el:
        result["seller_name"] = seller_el.get_text(strip=True)

    page_text = soup.get_text()
    if "Enviado pela Amazon" in page_text or "Dispatched from and sold by Amazon" in page_text:
        result["is_fba"] = True

    return result


def _scrape_kabum(soup):
    """
    KaBuM! parser — targets the main cash/PIX price card,
    explicitly ignoring instalment badge elements.
    """
    result = {"cash_price": None, "seller_name": "KaBuM!"}

    # Ordered CSS selectors: cash/PIX price first, most specific to least
    cash_selectors = [
        'span[class*="priceCard"]',       # main price card
        'span[class*="finalPrice"]',
        'b[class*="regularPrice"]',
        '[data-testid*="price"]:not([data-testid*="installment"])',
        'h4[class*="price"]',
        'span[class*="Price"]:not([class*="installment"]):not([class*="Installment"])',
    ]
    for sel in cash_selectors:
        el = soup.select_one(sel)
        if el and not _is_installment_context(el):
            price = clean_price(el.get_text())
            if price:
                result["cash_price"] = price
                break

    # Regex fallback: scan full-price patterns "R$ X.XXX,XX" from page text,
    # split by lines to avoid mixing instalment lines, take the minimum valid value.
    if result["cash_price"] is None:
        candidates = []
        for line in soup.get_text("\n").splitlines():
            if _INSTALLMENT_KEYWORDS.search(line):
                continue
            for match in re.finditer(r"R\$\s*[\d\.]+,\d{2}", line):
                price = clean_price(match.group())
                if price:
                    candidates.append(price)
        if candidates:
            result["cash_price"] = min(candidates)

    seller_el = (
        soup.select_one('[class*="sellerName"]') or
        soup.select_one('[class*="seller-name"]') or
        soup.select_one('[class*="SellerName"]')
    )
    if seller_el:
        result["seller_name"] = seller_el.get_text(strip=True)

    return result


def _scrape_magalu(soup, url):
    """
    Magazine Luiza parser — targets à vista / PIX price,
    skips instalment context elements.
    """
    result = {"cash_price": None, "seller_name": "Magalu"}

    cash_selectors = [
        '[data-testid="price-value"]',
        'p[data-testid="price-value"]',
        '[class*="price-value"]',
        '[class*="sc-dkzDqf"]',
    ]
    for sel in cash_selectors:
        el = soup.select_one(sel)
        if el and not _is_installment_context(el):
            price = clean_price(el.get_text())
            if price:
                result["cash_price"] = price
                break

    # Regex fallback with instalment line filtering
    if result["cash_price"] is None:
        candidates = []
        for line in soup.get_text("\n").splitlines():
            if _INSTALLMENT_KEYWORDS.search(line):
                continue
            for match in re.finditer(r"R\$\s*[\d\.]+,\d{2}", line):
                price = clean_price(match.group())
                if price:
                    candidates.append(price)
        if candidates:
            result["cash_price"] = min(candidates)

    seller_el = (
        soup.select_one('[data-testid="seller-info"]') or
        soup.select_one('[data-testid="seller-name"]') or
        soup.select_one('p[data-testid="seller-info"]')
    )
    if seller_el:
        result["seller_name"] = seller_el.get_text(strip=True)
    else:
        params = parse_qs(urlparse(url).query)
        seller_id = params.get("seller_id", [None])[0]
        if seller_id:
            result["seller_name"] = seller_id

    return result


# ---------------------------------------------------------------------------
# Core fetch + dispatch
# ---------------------------------------------------------------------------

def _scrape_url(url):
    """
    Fetches one URL, dispatches to the right parser, returns:
      { cash_price, seller_name, is_fba, url, platform }
    or None on failure / no valid price found.
    """
    platform = _detect_platform(url)
    headers_map = {"amazon": _AMAZON_HEADERS, "kabum": _KABUM_HEADERS, "magalu": _MAGALU_HEADERS}
    headers = headers_map.get(platform, _BASE_HEADERS)

    try:
        resp = _fetch(url, headers)
    except requests.RequestException as e:
        print(f"[Scraper] ✗ HTTP error ({platform.upper()}) — {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    jsonld_price = _parse_jsonld(soup)

    if platform == "amazon":
        data = _scrape_amazon(soup)
        if data["cash_price"] is None:
            data["cash_price"] = jsonld_price
        data.setdefault("is_fba", False)
    elif platform == "kabum":
        data = _scrape_kabum(soup)
        if data["cash_price"] is None:
            data["cash_price"] = jsonld_price
        data["is_fba"] = False
    elif platform == "magalu":
        data = _scrape_magalu(soup, url)
        if data["cash_price"] is None:
            data["cash_price"] = jsonld_price
        data["is_fba"] = False
    else:
        data = {"cash_price": jsonld_price, "seller_name": "Unknown", "is_fba": False}

    price = data.get("cash_price")

    # Final safety gate: reject anything below the minimum valid price
    if not price or price < MIN_VALID_PRICE:
        print(f"[Scraper] ✗ {platform.upper()}: price R$ {price} rejected (below R$ {MIN_VALID_PRICE:.0f} floor)")
        return None

    print(f"[Scraper] ✔ {platform.upper().ljust(6)} | {data['seller_name']} | R$ {price:.2f}")
    data["url"] = url
    data["platform"] = platform
    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scrape_product(product):
    """
    Scrapes every URL for the product, compares all valid results,
    and returns the one with the lowest cash price.
    """
    urls = product.get("urls", [])
    if not urls:
        print(f"[Scraper] No URLs defined for product_id={product['id']}")
        return None

    print(f"\n[Scraper] Scanning {len(urls)} URL(s) for '{product['name']}'")
    candidates = []
    for url in urls:
        result = _scrape_url(url)
        if result:
            candidates.append(result)

    if not candidates:
        print(f"[Scraper] No valid price found for product_id={product['id']}")
        return None

    best = min(candidates, key=lambda r: r["cash_price"])
    print(
        f"[Scraper] ★ Best price: R$ {best['cash_price']:.2f} "
        f"via {best['platform'].upper()} ({best['seller_name']})"
    )

    return {
        "product_id": product["id"],
        "product_name": product["name"],
        "seller_name": best["seller_name"],
        "is_fba": best.get("is_fba", False),
        "standard_price": best["cash_price"],
        "cash_price": best["cash_price"],
        "shipping_cost": 0.0,
        "total_effective_cost": best["cash_price"],
        "source_url": best["url"],
        "source_platform": best["platform"],
    }


def scrape_with_retry(product, max_attempts=3):
    """Runs scrape_product with up to max_attempts retries on total failure."""
    for attempt in range(1, max_attempts + 1):
        print(f"\n[Scraper] Attempt {attempt}/{max_attempts} — product_id={product['id']}")
        data = scrape_product(product)
        if data is not None:
            return data
        if attempt < max_attempts:
            print(f"[Scraper] Retrying in 5s...")
            time.sleep(5)
    print(f"[Scraper] All {max_attempts} attempts exhausted for product_id={product['id']}")
    return None
