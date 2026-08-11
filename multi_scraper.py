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

MIN_VALID_PRICE = 1500.0

_INSTALLMENT_KEYWORDS = re.compile(
    r"\b(em\s+até|\d+x|\bmensal\b|por\s+m[eê]s|parcela|prestação|vezes)\b",
    re.IGNORECASE,
)

# Patterns that indicate a Pix / cash discount is advertised on the page.
# Captures an optional explicit percentage (e.g. "5% de desconto no Pix").
_PIX_DISCOUNT_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*%\s*(?:de\s+)?(?:desconto\s+)?(?:off\s+)?(?:à\s+vista|no\s+pix|pix|nupay|nu\s*pay)",
    re.IGNORECASE,
)
# Fallback: generic "X% off à vista" / "X% no Pix" without "desconto" word
_PIX_DISCOUNT_RE2 = re.compile(
    r"(?:desconto|off|cashback)\s+(?:de\s+)?(\d+(?:[.,]\d+)?)\s*%\s*(?:à\s+vista|no\s+pix|pix|nupay)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clean_price(text):
    """
    Convert a Brazilian price string to float.
    'R$ 3.846,55' -> 3846.55
    Returns None if below MIN_VALID_PRICE or unparseable.
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
    Returns True if the element or up to 2 ancestor nodes contain
    instalment keywords (12x, em até, mensal, por mês …).
    """
    node = element
    for _ in range(3):
        if node is None:
            break
        if _INSTALLMENT_KEYWORDS.search(node.get_text(" ", strip=True)):
            return True
        node = node.parent
    return False


def _extract_pix_discount(page_text):
    """
    Scans the full page text for an advertised Pix/cash discount percentage.
    Returns the discount as a float fraction (e.g. 0.05 for 5%), or 0.0 if
    no discount is found.
    """
    for pattern in (_PIX_DISCOUNT_RE, _PIX_DISCOUNT_RE2):
        match = pattern.search(page_text)
        if match:
            raw = match.group(1).replace(",", ".")
            try:
                pct = float(raw)
                if 0 < pct <= 30:          # sanity: ignore absurd percentages
                    discount = round(pct / 100, 6)
                    print(f"[Scraper]   ↳ Pix/cash discount detected: {pct:.1f}%")
                    return discount
            except ValueError:
                pass
    return 0.0


def _apply_pix_discount(price, discount):
    """Apply a fractional discount and round to 2 decimal places."""
    if discount > 0:
        return round(price * (1 - discount), 2)
    return price


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

def _scrape_amazon(soup, page_text):
    """
    Amazon parser — 3-layer fallback:
      1. BuyBox main offer (standard product page)
      2. AOD offer list (#aod-offer-list) — all-offers page
      3. JSON-LD injected by caller

    After extracting the base price, detects any advertised Pix/NuPay/cash
    discount and applies it. BuyBox price is compared against the best
    marketplace offer; the lower of the two is returned.

    Returns dict { cash_price, base_price, pix_discount, seller_name, is_fba }.
    """
    result = {
        "cash_price": None,
        "base_price": None,
        "pix_discount": 0.0,
        "seller_name": "Amazon",
        "is_fba": False,
    }

    pix_discount = _extract_pix_discount(page_text)
    result["pix_discount"] = pix_discount

    # ------------------------------------------------------------------
    # 1. BuyBox — main offer (most reliable for the featured seller)
    # ------------------------------------------------------------------
    buybox_selectors = [
        "#corePrice_feature_div .a-offscreen",
        "#corePrice_feature_div .a-price .a-offscreen",
        "#apex_offerDisplay_desktop .a-price .a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "#price_inside_buybox",
        ".a-price .a-offscreen",
    ]
    buybox_price = None
    for sel in buybox_selectors:
        el = soup.select_one(sel)
        if el and not _is_installment_context(el):
            p = clean_price(el.get_text())
            if p:
                buybox_price = p
                break

    if buybox_price:
        result["base_price"] = buybox_price
        result["cash_price"] = _apply_pix_discount(buybox_price, pix_discount)

        seller_el = (
            soup.select_one("#sellerProfileTriggerId") or
            soup.select_one("#merchant-info a") or
            soup.select_one("#merchant-info")
        )
        if seller_el:
            result["seller_name"] = seller_el.get_text(strip=True)
        if "Enviado pela Amazon" in page_text or "Dispatched from and sold by Amazon" in page_text:
            result["is_fba"] = True

    # ------------------------------------------------------------------
    # 2. AOD offer list — compare against buybox and take the lower
    # ------------------------------------------------------------------
    aod_list = soup.select_one("#aod-offer-list")
    if aod_list:
        best_aod_price, best_aod_seller, best_aod_fba = None, "Amazon", False

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

            price = _apply_pix_discount(price, pix_discount)

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
            if best_aod_price is None or price < best_aod_price:
                best_aod_price, best_aod_seller, best_aod_fba = price, seller, fba

        # Keep whichever is cheaper: buybox or best marketplace offer
        if best_aod_price and (result["cash_price"] is None or best_aod_price < result["cash_price"]):
            result["cash_price"] = best_aod_price
            result["seller_name"] = best_aod_seller
            result["is_fba"] = best_aod_fba
            print(f"[Scraper]   ↳ AOD offer cheaper than BuyBox — using AOD price")

    return result


def _scrape_kabum(soup):
    """KaBuM! parser — targets PIX/cash price, ignores instalment badges."""
    result = {"cash_price": None, "seller_name": "KaBuM!"}

    cash_selectors = [
        'span[class*="priceCard"]',
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
    """Magalu parser — targets à vista / PIX price, skips instalment elements."""
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
    or None on failure / no valid price.
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
    page_text = soup.get_text(" ", strip=True)
    jsonld_price = _parse_jsonld(soup)

    if platform == "amazon":
        data = _scrape_amazon(soup, page_text)
        if data["cash_price"] is None:
            # JSON-LD base price — still apply any detected Pix discount
            if jsonld_price:
                data["cash_price"] = _apply_pix_discount(jsonld_price, data.get("pix_discount", 0.0))
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
    if not price or price < MIN_VALID_PRICE:
        print(f"[Scraper] ✗ {platform.upper()}: R$ {price} rejected (below R$ {MIN_VALID_PRICE:.0f} floor)")
        return None

    # Log base vs cash price when a Pix discount was applied
    base = data.get("base_price")
    if base and base != price:
        print(f"[Scraper] ✔ {platform.upper().ljust(6)} | {data['seller_name']} | R$ {price:.2f}  (base R$ {base:.2f} - Pix discount)")
    else:
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
        "standard_price": best.get("base_price") or best["cash_price"],
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
