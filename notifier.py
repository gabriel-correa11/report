import io
import json
import sqlite3
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import requests

from config import DATABASE_URL, DISCORD_WEBHOOK_URL

_USE_SQLITE = DATABASE_URL is None or DATABASE_URL.startswith("sqlite")

_BG_COLOR      = "#2F3136"
_SURFACE_COLOR = "#36393F"
_TEXT_COLOR    = "#DCDDDE"
_GRID_COLOR    = "#40444B"
_LINE_COLOR    = "#5865F2"

# Badge assigned to each platform rank in the price list
_RANK_BADGES = ["🟢", "🔵", "🟡", "🟠", "🔴"]

# Human-readable platform display names
_PLATFORM_LABELS = {
    "amazon": "Amazon",
    "kabum":  "KaBuM!",
    "magalu": "Magazine Luiza",
}


def _fetch_7d_history(product_id):
    """Returns list of (created_at, total_effective_cost) for the last 7 days."""
    if _USE_SQLITE:
        db_path = DATABASE_URL.replace("sqlite:///", "") if DATABASE_URL else "prices.db"
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT created_at, total_effective_cost
            FROM price_history
            WHERE product_id = ?
              AND created_at >= datetime('now', '-7 days')
            ORDER BY created_at ASC
            """,
            (product_id,),
        )
        rows = cursor.fetchall()
        conn.close()
        result = []
        for ts, price in rows:
            try:
                dt = datetime.fromisoformat(ts)
            except ValueError:
                dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            result.append((dt, float(price)))
        return result
    else:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT created_at, total_effective_cost
            FROM price_history
            WHERE product_id = %s
              AND created_at >= NOW() - INTERVAL '7 DAYS'
            ORDER BY created_at ASC
            """,
            (product_id,),
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return [(dt, float(price)) for dt, price in rows]


def generate_price_chart(product_id, product_name):
    """
    Queries 7-day price history and produces a dark-themed line chart.
    Returns PNG bytes (io.BytesIO) or None if there is insufficient data.
    """
    history = _fetch_7d_history(product_id)
    if not history:
        return None

    dates  = [row[0] for row in history]
    prices = [row[1] for row in history]

    fig, ax = plt.subplots(figsize=(10, 4.5), facecolor=_BG_COLOR)
    ax.set_facecolor(_SURFACE_COLOR)

    ax.plot(dates, prices, color=_LINE_COLOR, linewidth=2.5, marker="o",
            markersize=4, markerfacecolor=_LINE_COLOR, zorder=3)
    ax.fill_between(dates, prices, alpha=0.12, color=_LINE_COLOR)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m %Hh"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=8))
    fig.autofmt_xdate(rotation=30, ha="right")

    ax.yaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(
            lambda val, _: f"R$ {val:,.0f}".replace(",", ".")
        )
    )

    for spine in ax.spines.values():
        spine.set_edgecolor(_GRID_COLOR)
    ax.tick_params(colors=_TEXT_COLOR, labelsize=9)
    ax.yaxis.label.set_color(_TEXT_COLOR)
    ax.xaxis.label.set_color(_TEXT_COLOR)
    ax.grid(color=_GRID_COLOR, linewidth=0.7, linestyle="-", alpha=0.6)

    ax.set_title(
        f"Histórico de Preços — {product_name} (7 dias)",
        color=_TEXT_COLOR,
        fontsize=12,
        pad=12,
    )

    plt.tight_layout(pad=1.5)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=_BG_COLOR)
    plt.close(fig)
    buf.seek(0)
    return buf


def _fmt_brl(value):
    """3846.55 -> 'R$ 3.846,55'"""
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _build_price_list(candidates):
    """
    Given a list of candidate dicts (sorted cheapest first), build a
    Discord markdown string listing every store with badge, seller, price,
    and a clickable link.

    Example output line:
      🟢 **Amazon** (Over Power / FBA): R$ 3.846,55 — [Ir para a oferta](url)
    """
    lines = []
    for i, c in enumerate(candidates):
        badge        = _RANK_BADGES[i] if i < len(_RANK_BADGES) else "⚪"
        platform_lbl = _PLATFORM_LABELS.get(c["platform"], c["platform"].capitalize())
        seller       = c.get("seller_name") or platform_lbl
        price_str    = _fmt_brl(c["cash_price"])
        url          = c.get("url", "")
        fba_tag      = " / FBA" if c.get("is_fba") else ""

        # Avoid duplicating the platform name when the seller IS the platform
        if seller.lower().replace("!", "") == platform_lbl.lower().replace("!", ""):
            label = f"**{platform_lbl}**{fba_tag}"
        else:
            label = f"**{platform_lbl}** ({seller}{fba_tag})"

        link = f"[Ir para a oferta]({url})" if url else ""
        line = f"{badge} {label}: {price_str}"
        if link:
            line += f" — {link}"
        lines.append(line)

    return "\n".join(lines) if lines else "Nenhuma oferta encontrada."


def send_discord_alert(product, analytics, seller_info):
    """
    Sends a Discord embed listing all scanned prices with the historical
    chart attached at the bottom. Called on every hourly run.
    """
    if not DISCORD_WEBHOOK_URL:
        print("[Notifier] DISCORD_WEBHOOK_URL not set. Skipping notification.")
        return

    candidates    = seller_info.get("all_candidates", [])
    best_price    = candidates[0]["cash_price"] if candidates else (analytics.get("current_cash_price") or 0.0)
    max_threshold = product.get("max_alert_threshold", 3800.00)

    if best_price <= max_threshold:
        embed_color = 5763719   # Green — price within opportunity range
    else:
        embed_color = 3447003   # Blue  — regular hourly report

    embed_title = f"📊 RELATÓRIO DE VARREDURA HOURLY - {product['name']}"

    price_list_text = _build_price_list(candidates)

    embed = {
        "title": embed_title,
        "color": embed_color,
        "fields": [
            {
                "name": "🛒 Preços Encontrados no Momento:",
                "value": price_list_text,
                "inline": False,
            },
        ],
        "image": {"url": "attachment://chart.png"},
        "footer": {
            "text": f"Price Tracker • {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M UTC')}"
        },
    }

    payload   = {"embeds": [embed]}
    chart_buf = generate_price_chart(product["id"], product["name"])

    try:
        if chart_buf:
            files    = {"file": ("chart.png", chart_buf, "image/png")}
            response = requests.post(
                DISCORD_WEBHOOK_URL,
                data={"payload_json": json.dumps(payload)},
                files=files,
                timeout=20,
            )
        else:
            del embed["image"]
            response = requests.post(
                DISCORD_WEBHOOK_URL,
                json=payload,
                timeout=20,
            )

        response.raise_for_status()
        print(f"[Notifier] Discord alert sent for product_id={product['id']}")

    except requests.RequestException as e:
        print(f"[Notifier] Failed to send Discord alert for product_id={product['id']}: {e}")
