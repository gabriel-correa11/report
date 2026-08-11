import sqlite3
from config import DATABASE_URL

_USE_SQLITE = DATABASE_URL is None or DATABASE_URL.startswith("sqlite")


def _get_connection():
    if _USE_SQLITE:
        db_path = DATABASE_URL.replace("sqlite:///", "") if DATABASE_URL else "price_tracker.db"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn
    else:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DATABASE_URL)
        return conn


def setup_db():
    conn = _get_connection()
    cursor = conn.cursor()

    if _USE_SQLITE:
        schema = """
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id VARCHAR(50) NOT NULL,
            product_name VARCHAR(255) NOT NULL,
            seller_name VARCHAR(255),
            is_fba BOOLEAN DEFAULT 0,
            standard_price NUMERIC(10, 2),
            cash_price NUMERIC(10, 2),
            shipping_cost NUMERIC(10, 2) DEFAULT 0.00,
            total_effective_cost NUMERIC(10, 2) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    else:
        schema = """
        CREATE TABLE IF NOT EXISTS price_history (
            id SERIAL PRIMARY KEY,
            product_id VARCHAR(50) NOT NULL,
            product_name VARCHAR(255) NOT NULL,
            seller_name VARCHAR(255),
            is_fba BOOLEAN DEFAULT FALSE,
            standard_price NUMERIC(10, 2),
            cash_price NUMERIC(10, 2),
            shipping_cost NUMERIC(10, 2) DEFAULT 0.00,
            total_effective_cost NUMERIC(10, 2) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """

    cursor.execute(schema)
    conn.commit()
    cursor.close()
    conn.close()
    print("[DB] Schema initialized.")


def save_price_record(data_dict):
    conn = _get_connection()
    cursor = conn.cursor()

    sql = """
    INSERT INTO price_history
        (product_id, product_name, seller_name, is_fba, standard_price, cash_price, shipping_cost, total_effective_cost)
    VALUES
        (%(product_id)s, %(product_name)s, %(seller_name)s, %(is_fba)s, %(standard_price)s, %(cash_price)s, %(shipping_cost)s, %(total_effective_cost)s)
    """

    if _USE_SQLITE:
        sql = """
        INSERT INTO price_history
            (product_id, product_name, seller_name, is_fba, standard_price, cash_price, shipping_cost, total_effective_cost)
        VALUES
            (:product_id, :product_name, :seller_name, :is_fba, :standard_price, :cash_price, :shipping_cost, :total_effective_cost)
        """

    cursor.execute(sql, data_dict)
    conn.commit()
    cursor.close()
    conn.close()
    print(f"[DB] Record saved for product_id={data_dict.get('product_id')}")


def get_product_analytics(product_id):
    conn = _get_connection()
    cursor = conn.cursor()

    if _USE_SQLITE:
        queries = {
            "current_cash_price": """
                SELECT total_effective_cost FROM price_history
                WHERE product_id = ?
                ORDER BY created_at DESC LIMIT 1
            """,
            "low_24h": """
                SELECT MIN(total_effective_cost) FROM price_history
                WHERE product_id = ?
                AND created_at >= datetime('now', '-24 hours')
            """,
            "low_7d": """
                SELECT MIN(total_effective_cost) FROM price_history
                WHERE product_id = ?
                AND created_at >= datetime('now', '-7 days')
            """,
            "date_low_7d": """
                SELECT created_at FROM price_history
                WHERE product_id = ?
                AND created_at >= datetime('now', '-7 days')
                ORDER BY total_effective_cost ASC LIMIT 1
            """,
            "avg_7d": """
                SELECT AVG(total_effective_cost) FROM price_history
                WHERE product_id = ?
                AND created_at >= datetime('now', '-7 days')
            """,
            "all_time_low": """
                SELECT MIN(total_effective_cost) FROM price_history
                WHERE product_id = ?
            """,
        }
        results = {}
        for key, sql in queries.items():
            cursor.execute(sql, (product_id,))
            row = cursor.fetchone()
            results[key] = row[0] if row else None
    else:
        sql = """
        SELECT
            (SELECT total_effective_cost FROM price_history
             WHERE product_id = %(pid)s ORDER BY created_at DESC LIMIT 1) AS current_cash_price,

            (SELECT MIN(total_effective_cost) FROM price_history
             WHERE product_id = %(pid)s
             AND created_at >= NOW() - INTERVAL '24 HOURS') AS low_24h,

            (SELECT MIN(total_effective_cost) FROM price_history
             WHERE product_id = %(pid)s
             AND created_at >= NOW() - INTERVAL '7 DAYS') AS low_7d,

            (SELECT created_at FROM price_history
             WHERE product_id = %(pid)s
             AND created_at >= NOW() - INTERVAL '7 DAYS'
             ORDER BY total_effective_cost ASC LIMIT 1) AS date_low_7d,

            (SELECT AVG(total_effective_cost) FROM price_history
             WHERE product_id = %(pid)s
             AND created_at >= NOW() - INTERVAL '7 DAYS') AS avg_7d,

            (SELECT MIN(total_effective_cost) FROM price_history
             WHERE product_id = %(pid)s) AS all_time_low
        """
        cursor.execute(sql, {"pid": product_id})
        row = cursor.fetchone()
        if row:
            cols = [desc[0] for desc in cursor.description]
            results = dict(zip(cols, row))
        else:
            results = {
                "current_cash_price": None,
                "low_24h": None,
                "low_7d": None,
                "date_low_7d": None,
                "avg_7d": None,
                "all_time_low": None,
            }

    cursor.close()
    conn.close()
    return results
