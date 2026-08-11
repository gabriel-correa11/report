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

# Realistic browser headers — shared baseline for all platforms
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

_AMAZON_HEADERS = {
    **_BASE_HEADERS,
    "Referer": "https://www.amazon.com.br/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

_KABUM_HEADERS = {
    **_BASE_HEADERS,
    "Referer": "https://www.kabum.com.br/",
}

_MAGALU_HEADERS = {
    **_BASE_HEADERS,
    "Referer": "https://www.magazineluiza.com.br/",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clean_price(text):
    """
    Sanitize a Brazilian price string to float.
    'R$ 3.846,55' -> 3846.55   |   '3800' -> 3800.0
    """
    if not text:
        return None
    text = text.strip()
    # Remove everything except digits and comma
    cleaned = re.sub(r"[^\d,]", "", text)
    if not cleaned:
        return None
    if "," in cleaned:
        # Brazilian decimal: last comma is decimal separator, dots are thousand sep
        parts = cleaned.rsplit(",", 1)
        integer_part = parts[0].replace(",", "")
        decimal_part = parts[1]
        cleaned = f"{integer_part}.{decimal_part}"
    try:
        val = float(cleaned)
        # Sanity: reject suspiciously small values (e.g. fragment "3,00" matching installments)
        return val if val >= 10.0 else None
    except ValueError:
        return None


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
    """Shared HTTP fetch using the persistent session."""
    resp = _SESSION.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    return resp


def _parse_jsonld(soup):
    """Extract lowest price from JSON-LD structured data."""
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
                    if val >= 10 and (best is None or val < best):
                        best = val
                except ValueError:
                    pass
    return best


# ---------------------------------------------------------------------------
# Platform-specific parsers
# ---------------------------------------------------------------------------

def _scrape_amazon(soup):
    """
    Amazon parser.

    Priority order:
      1. AOD offer list (#aod-offer-list) — present when ?aod=1 is rendered
      2. Buy-box price selectors (corePrice, priceblock, .a-price .a-offscreen)
      3. JSON-LD (injected by caller if DOM misses)

    Returns dict { cash_price, seller_name, is_fba } or all-None on miss.
    """
    result = {"cash_price": None, "seller_name": "Amazon", "is_fba": False}

    # 1. AOD offer list
    aod_list = soup.select_one("#aod-offer-list")
    if aod_list:
        best_price, best_seller, best_fba = None, "Amazon", False
        for offer in aod_list.select("#aod-offer"):
            # .a-offscreen holds the full price text (e.g. "R$ 3.800,00")
            price_el = offer.select_one(".a-offscreen")
            price = clean_price(price_el.get_text()) if price_el else None
            if not price:
                # Fallback: whole + fraction
                whole = offer.select_one(".a-price-whole")
                frac  = offer.select_one(".a-price-fraction")
                if whole:
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

    # 2. Buy-box selectors (rendered page / standard product page)
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
        if el:
            price = clean_price(el.get_text())
            if price:
                result["cash_price"] = price
                break

    # Seller
    seller_el = (
        soup.select_one("#sellerProfileTriggerId") or
        soup.select_one("#merchant-info a") or
        soup.select_one("#merchant-info")
    )
    if seller_el:
        result["seller_name"] = seller_el.get_text(strip=True)

    # FBA flag
    page_text = soup.get_text()
    if "Enviado pela Amazon" in page_text or "Dispatched from and sold by Amazon" in page_text:
        result["is_fba"] = True

    return result


def _scrape_kabum(soup):
    """
    KaBuM! parser — prioritises PIX / cash price over instalment price.
    Returns dict { cash_price, seller_name }.
    """
    result = {"cash_price": None, "seller_name": "KaBuM!"}

    # Ordered CSS selectors: most-specific cash/PIX price first
    cash_selectors = [
        'span[class*="finalPrice"]',
        'span[class*="priceCard"]',
        'b[class*="regularPrice"]',
        '[class*="cash"] [class*="price"]',
        '[data-testid*="price"]',
        'h4[class*="price"]',
        'span[class*="Price"]',
    ]
    for sel in cash_selectors:
        el = soup.select_one(sel)
        if el:
            price = clean_price(el.get_text())
            if price and price > 50:
                result["cash_price"] = price
                break

    # Regex fallback: scan visible text for "R$ X.XXX,XX" patterns and take the lowest
    if result["cash_price"] is None:
        candidates = []
        for match in re.finditer(r"R\$\s*[\d\.]+,\d{2}", soup.get_text()):
            price = clean_price(match.group())
            if price and price > 50:
                candidates.append(price)
        if candidates:
            result["cash_price"] = min(candidates)

    # Seller
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
    Magazine Luiza parser — prioritises à vista / PIX price.
    Returns dict { cash_price, seller_name }.
    """
    result = {"cash_price": None, "seller_name": "Magalu"}

    # Ordered: PIX/cash price before instalment
    cash_selectors = [
        '[data-testid="price-value"]',
        '[data-testid="installment-price"] ~ [data-testid="price-value"]',
        'p[data-testid="price-value"]',
        '[class*="price-value"]',
        '[class*="sc-dkzDqf"]',   # Magalu styled-component cash price
    ]
    for sel in cash_selectors:
        el = soup.select_one(sel)
        if el:
            price = clean_price(el.get_text())
            if price and price > 50:
                result["cash_price"] = price
                break

    # Regex fallback over page text (same as Kabum — take lowest R$ match)
    if result["cash_price"] is None:
        candidates = []
        for match in re.finditer(r"R\$\s*[\d\.]+,\d{2}", soup.get_text()):
            price = clean_price(match.group())
            if price and price > 50:
                candidates.append(price)
        if candidates:
            result["cash_price"] = min(candidates)

    # Seller
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
    Fetches one URL, dispatches to the right parser, and returns:
      { cash_price, seller_name, is_fba, url, platform }
    or None on failure / no price found.
    """
    platform = _detect_platform(url)

    # Pick per-platform headers
    headers_map = {
        "amazon": _AMAZON_HEADERS,
        "kabum":  _KABUM_HEADERS,
        "magalu": _MAGALU_HEADERS,
    }
    headers = headers_map.get(platform, _BASE_HEADERS)

    try:
        resp = _fetch(url, headers)
    except requests.RequestException as e:
        print(f"[Scraper] ✗ HTTP error ({platform}) {url}: {e}")
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
        data = {
            "cash_price": jsonld_price,
            "seller_name": "Unknown",
            "is_fba": False,
        }

    price = data.get("cash_price")
    if not price:
        print(f"[Scraper] ✗ No price found — {platform.upper()}: {url}")
        return None

    label = platform.upper().ljust(6)
    print(f"[Scraper] ✔ {label} | {data['seller_name']} | R$ {price:.2f}")

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
