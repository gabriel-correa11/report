import re
import json
import time
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}


def clean_price(text):
    """Sanitize a Brazilian price string to float. e.g. 'R$ 3.846,55' -> 3846.55"""
    if not text:
        return None
    cleaned = re.sub(r"[^\d,]", "", text.strip())
    if "," in cleaned:
        parts = cleaned.rsplit(",", 1)
        integer_part = parts[0].replace(",", "")
        decimal_part = parts[1]
        cleaned = f"{integer_part}.{decimal_part}"
    try:
        return float(cleaned)
    except ValueError:
        return None


def _detect_platform(url):
    """Infer platform name from URL hostname."""
    host = urlparse(url).hostname or ""
    if "amazon" in host:
        return "amazon"
    if "kabum" in host:
        return "kabum"
    if "magazineluiza" in host or "magalu" in host:
        return "magalu"
    return "generic"


def _parse_jsonld(soup):
    """Primary parser: extract lowest price from JSON-LD structured data."""
    scripts = soup.find_all("script", type="application/ld+json")
    best = None
    for script in scripts:
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        items = data if isinstance(data, list) else [data]
        for item in items:
            item_type = item.get("@type", "")
            price = None
            if item_type == "Product":
                offers = item.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                price = offers.get("price") or offers.get("lowPrice")
            elif item_type in ("Offer", "AggregateOffer"):
                price = item.get("price") or item.get("lowPrice")
            if price:
                try:
                    val = float(str(price).replace(",", "."))
                    if best is None or val < best:
                        best = val
                except ValueError:
                    pass
    return best


# ---------------------------------------------------------------------------
# Platform-specific parsers
# ---------------------------------------------------------------------------

def _scrape_amazon(soup, url):
    """
    Amazon parser. For AOD pages (aod=1), iterates all offer rows in
    #aod-offer-list and picks the lowest. Falls back to buy-box selectors.
    Returns dict with cash_price, seller_name, is_fba.
    """
    result = {"cash_price": None, "seller_name": "Amazon", "is_fba": False}

    # --- All Offers Display (aod=1) ---
    aod_list = soup.select_one("#aod-offer-list")
    if aod_list:
        best_price = None
        best_seller = "Amazon"
        best_fba = False
        for offer in aod_list.select("#aod-offer"):
            price_el = offer.select_one(".a-offscreen") or offer.select_one(".a-price-whole")
            price = clean_price(price_el.get_text()) if price_el else None
            if price is None:
                continue
            seller_el = offer.select_one("#aod-offer-soldBy .a-size-small") or \
                        offer.select_one("#aod-offer-soldBy span")
            seller = seller_el.get_text(strip=True) if seller_el else "Amazon"
            offer_text = offer.get_text()
            fba = ("Enviado pela Amazon" in offer_text or
                   "Fulfilled by Amazon" in offer_text or
                   "Amazon" in seller)
            if best_price is None or price < best_price:
                best_price = price
                best_seller = seller
                best_fba = fba
        if best_price:
            result["cash_price"] = best_price
            result["seller_name"] = best_seller
            result["is_fba"] = best_fba
            return result

    # --- Standard buy-box fallback ---
    price_el = soup.select_one("#corePrice_feature_div .a-offscreen") or \
               soup.select_one("#priceblock_ourprice")
    if price_el:
        result["cash_price"] = clean_price(price_el.get_text())

    seller_el = soup.select_one("#sellerProfileTriggerId") or \
                soup.select_one("#merchant-info")
    if seller_el:
        result["seller_name"] = seller_el.get_text(strip=True)

    page_text = soup.get_text()
    if "Enviado pela Amazon" in page_text or "Dispatched from and sold by Amazon" in page_text:
        result["is_fba"] = True

    return result


def _scrape_kabum(soup):
    """
    Kabum parser. Extracts cash/PIX price and seller name.
    Returns dict with cash_price, seller_name.
    """
    result = {"cash_price": None, "seller_name": "KaBuM!"}

    # Cash / PIX price selectors
    for selector in [
        'span[class*="finalPrice"]',
        'span[class*="priceCard"]',
        'b[class*="regularPrice"]',
        'span[class*="sc-"]',   # generic styled-component span with price-like text
    ]:
        el = soup.select_one(selector)
        if el:
            price = clean_price(el.get_text())
            if price and price > 1:
                result["cash_price"] = price
                break

    # Fallback: find any element whose text looks like a BRL price
    if result["cash_price"] is None:
        for el in soup.find_all(string=re.compile(r"R\$\s*[\d\.]+")):
            price = clean_price(el)
            if price and price > 100:
                result["cash_price"] = price
                break

    # Seller (marketplace info)
    seller_el = soup.select_one('[class*="sellerName"]') or \
                soup.select_one('[class*="seller-name"]')
    if seller_el:
        result["seller_name"] = seller_el.get_text(strip=True)

    return result


def _scrape_magalu(soup, url):
    """
    Magazine Luiza parser. Extracts cash price and seller from DOM or URL param.
    Returns dict with cash_price, seller_name.
    """
    result = {"cash_price": None, "seller_name": "Magalu"}

    cash_el = soup.select_one('p[data-testid="price-value"]') or \
              soup.select_one('[data-testid="price-value"]')
    if cash_el:
        result["cash_price"] = clean_price(cash_el.get_text())

    seller_el = soup.select_one('p[data-testid="seller-info"]') or \
                soup.select_one('[data-testid="seller-name"]')
    if seller_el:
        result["seller_name"] = seller_el.get_text(strip=True)
    else:
        params = parse_qs(urlparse(url).query)
        seller_id = params.get("seller_id", [None])[0]
        if seller_id:
            result["seller_name"] = seller_id

    return result


# ---------------------------------------------------------------------------
# Core scraping logic
# ---------------------------------------------------------------------------

def _scrape_url(url):
    """
    Fetches a single URL and returns a raw result dict:
      { cash_price, seller_name, is_fba, url, platform }
    or None on failure.
    """
    platform = _detect_platform(url)
    try:
        response = requests.get(url, headers=HEADERS, timeout=25)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"[Scraper] HTTP error for {url}: {e}")
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    jsonld_price = _parse_jsonld(soup)

    if platform == "amazon":
        data = _scrape_amazon(soup, url)
        # JSON-LD can be more reliable for buy-box; use it if DOM failed
        if data["cash_price"] is None and jsonld_price:
            data["cash_price"] = jsonld_price
        data["is_fba"] = data.get("is_fba", False)

    elif platform == "kabum":
        data = _scrape_kabum(soup)
        if data["cash_price"] is None and jsonld_price:
            data["cash_price"] = jsonld_price
        data["is_fba"] = False

    elif platform == "magalu":
        data = _scrape_magalu(soup, url)
        if data["cash_price"] is None and jsonld_price:
            data["cash_price"] = jsonld_price
        data["is_fba"] = False

    else:
        data = {
            "cash_price": jsonld_price,
            "seller_name": "Unknown",
            "is_fba": False,
        }

    if not data.get("cash_price"):
        print(f"[Scraper] No price found at {url}")
        return None

    data["url"] = url
    data["platform"] = platform
    return data


def scrape_product(product):
    """
    Scrapes all URLs for a product, compares results, and returns the
    overall lowest price with its source URL and seller info.
    """
    urls = product.get("urls", [])
    if not urls:
        print(f"[Scraper] No URLs defined for product_id={product['id']}")
        return None

    candidates = []
    for url in urls:
        print(f"[Scraper] Fetching {url}")
        result = _scrape_url(url)
        if result:
            candidates.append(result)
            print(f"[Scraper]  -> {result['platform']} | {result['seller_name']} | R$ {result['cash_price']:.2f}")

    if not candidates:
        return None

    # Pick the lowest cash price across all platforms
    best = min(candidates, key=lambda r: r["cash_price"])

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
    """Scrapes a product with up to max_attempts retries."""
    for attempt in range(1, max_attempts + 1):
        print(f"[Scraper] Attempt {attempt}/{max_attempts} for product_id={product['id']}")
        data = scrape_product(product)
        if data is not None:
            return data
        if attempt < max_attempts:
            time.sleep(5)
    print(f"[Scraper] All {max_attempts} attempts failed for product_id={product['id']}")
    return None
