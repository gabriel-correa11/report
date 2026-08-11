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
    # Handle Brazilian format: 3.846,55 -> 3846.55
    # After stripping non-numeric/non-comma, we may have: 384655 or 384655 or 384655
    # We need to detect if there are commas and treat last comma as decimal separator
    if "," in cleaned:
        # Everything before last comma is integer part (strip internal dots already removed), after is decimal
        parts = cleaned.rsplit(",", 1)
        integer_part = parts[0].replace(",", "")
        decimal_part = parts[1]
        cleaned = f"{integer_part}.{decimal_part}"
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_jsonld(soup):
    """Primary parser: extract price from JSON-LD structured data."""
    scripts = soup.find_all("script", type="application/ld+json")
    for script in scripts:
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        items = data if isinstance(data, list) else [data]
        for item in items:
            item_type = item.get("@type", "")
            # Handle Product type with nested offers
            if item_type == "Product":
                offers = item.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                price = offers.get("price") or offers.get("lowPrice")
                if price:
                    try:
                        return float(str(price).replace(",", "."))
                    except ValueError:
                        pass
            # Handle standalone Offer type
            if item_type in ("Offer", "AggregateOffer"):
                price = item.get("price") or item.get("lowPrice")
                if price:
                    try:
                        return float(str(price).replace(",", "."))
                    except ValueError:
                        pass
    return None


def _scrape_amazon(soup, product):
    """Amazon-specific DOM fallback parser."""
    result = {
        "cash_price": None,
        "standard_price": None,
        "seller_name": "Amazon",
        "is_fba": False,
        "shipping_cost": 0.0,
    }

    # Price extraction
    price_el = soup.select_one("#corePrice_feature_div .a-offscreen")
    if not price_el:
        price_el = soup.select_one("#priceblock_ourprice")
    if price_el:
        result["cash_price"] = clean_price(price_el.get_text())
        result["standard_price"] = result["cash_price"]

    # Seller name
    seller_el = soup.select_one("#sellerProfileTriggerId")
    if not seller_el:
        seller_el = soup.select_one("#merchant-info")
    if seller_el:
        result["seller_name"] = seller_el.get_text(strip=True)

    # FBA detection
    page_text = soup.get_text()
    if "Enviado pela Amazon" in page_text or "Dispatched from and sold by Amazon" in page_text:
        result["is_fba"] = True

    # Seller rating check
    rating_match = re.search(r"(\d{1,3})%\s*positiv", page_text, re.IGNORECASE)
    if rating_match:
        rating = int(rating_match.group(1))
        min_rating = product.get("min_seller_rating", 0)
        if rating < min_rating:
            print(f"[Scraper] Amazon seller rating {rating}% below minimum {min_rating}%. Discarding offer.")
            return None

    return result


def _scrape_magalu(soup, product_url):
    """Magazine Luiza-specific DOM fallback parser."""
    result = {
        "cash_price": None,
        "standard_price": None,
        "seller_name": None,
        "is_fba": False,
        "shipping_cost": 0.0,
    }

    # Cash price
    cash_el = soup.select_one('p[data-testid="price-value"]')
    if cash_el:
        result["cash_price"] = clean_price(cash_el.get_text())

    # Standard/original price
    std_el = soup.select_one('p[data-testid="price-original"]')
    if std_el:
        result["standard_price"] = clean_price(std_el.get_text())
    else:
        result["standard_price"] = result["cash_price"]

    # Seller name: try DOM first, then URL param
    seller_el = soup.select_one('p[data-testid="seller-info"]')
    if seller_el:
        result["seller_name"] = seller_el.get_text(strip=True)
    else:
        parsed = urlparse(product_url)
        params = parse_qs(parsed.query)
        seller_id = params.get("seller_id", [None])[0]
        result["seller_name"] = seller_id or "Magalu"

    return result


def scrape_product(product):
    """
    Attempts to scrape price data for a product.
    Returns a dict with price fields or None if scraping failed.
    """
    url = product["url"]
    platform = product.get("platform", "").lower()

    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"[Scraper] HTTP error for {url}: {e}")
        return None

    soup = BeautifulSoup(response.text, "html.parser")

    # --- Primary: JSON-LD ---
    jsonld_price = _parse_jsonld(soup)

    platform_data = None

    # --- Fallback: platform-specific parsers ---
    if platform == "amazon":
        platform_data = _scrape_amazon(soup, product)
        if platform_data is None:
            return None  # Discarded due to seller rating
        if jsonld_price and platform_data["cash_price"] is None:
            platform_data["cash_price"] = jsonld_price
            platform_data["standard_price"] = jsonld_price
        elif jsonld_price:
            platform_data["cash_price"] = jsonld_price

    elif platform == "magalu":
        platform_data = _scrape_magalu(soup, url)
        if jsonld_price and platform_data["cash_price"] is None:
            platform_data["cash_price"] = jsonld_price
            platform_data["standard_price"] = jsonld_price
        elif jsonld_price:
            platform_data["cash_price"] = jsonld_price

    else:
        # Generic: JSON-LD only
        if jsonld_price:
            platform_data = {
                "cash_price": jsonld_price,
                "standard_price": jsonld_price,
                "seller_name": "Unknown",
                "is_fba": False,
                "shipping_cost": 0.0,
            }

    if platform_data is None or platform_data.get("cash_price") is None:
        print(f"[Scraper] Could not extract price for product_id={product['id']}")
        return None

    shipping = platform_data.get("shipping_cost") or 0.0
    total = platform_data["cash_price"] + shipping

    return {
        "product_id": product["id"],
        "product_name": product["name"],
        "seller_name": platform_data.get("seller_name"),
        "is_fba": platform_data.get("is_fba", False),
        "standard_price": platform_data.get("standard_price"),
        "cash_price": platform_data["cash_price"],
        "shipping_cost": shipping,
        "total_effective_cost": total,
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
