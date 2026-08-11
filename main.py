import json
import sys

from db_manager import setup_db, save_price_record, get_product_analytics
from multi_scraper import scrape_with_retry
from notifier import send_discord_alert


def main():
    # 1. Initialize database schema
    setup_db()

    # 2. Load products
    with open("products.json", "r", encoding="utf-8") as f:
        products = json.load(f)

    print(f"[Main] Loaded {len(products)} product(s).")

    # 3. Process each product
    for product in products:
        print(f"\n[Main] Processing product_id={product['id']} ({product['name']})")
        try:
            # a. Scrape all URLs, return lowest price across platforms
            data = scrape_with_retry(product, max_attempts=3)

            if data is None:
                print(f"[Main] Skipping product_id={product['id']} — no price data found.")
                continue

            # b. Persist price record
            save_price_record(data)

            # c. Fetch analytics
            analytics = get_product_analytics(product["id"])

            seller_info = {
                "seller_name": data.get("seller_name"),
                "is_fba": data.get("is_fba", False),
                "source_url": data.get("source_url"),
                "source_platform": data.get("source_platform"),
            }

            # d. Always send Discord notification
            send_discord_alert(product, analytics, seller_info)

        except Exception as e:
            print(f"[Main] ERROR processing product_id={product['id']}: {e}")
            continue

    print("\n[Main] All products processed. Exiting.")
    sys.exit(0)


if __name__ == "__main__":
    main()
