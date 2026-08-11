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

_BG_COLOR = "#2F3136"
_SURFACE_COLOR = "#36393F"
_TEXT_COLOR = "#DCDDDE"
_GRID_COLOR = "#40444B"
_LINE_COLOR = "#5865F2"
_THRESHOLD_COLOR = "#ED4245"


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


def generate_price_chart(product_id, product_name, max_alert_threshold):
    """
    Queries 7-day price history and produces a dark-themed line chart.
    Returns PNG bytes (io.BytesIO) or None if there is insufficient data.
    """
    history = _fetch_7d_history(product_id)
    if not history:
        return None

    dates = [row[0] for row in history]
    prices = [row[1] for row in history]

    fig, ax = plt.subplots(figsize=(10, 4.5), facecolor=_BG_COLOR)
    ax.set_facecolor(_SURFACE_COLOR)

    ax.plot(dates, prices, color=_LINE_COLOR, linewidth=2.5, marker="o",
            markersize=4, markerfacecolor=_LINE_COLOR, zorder=3)
    ax.fill_between(dates, prices, alpha=0.12, color=_LINE_COLOR)

    ax.axhline(
        y=max_alert_threshold,
        color=_THRESHOLD_COLOR,
        linewidth=1.5,
        linestyle="--",
        label=f"Teto R$ {max_alert_threshold:,.0f}".replace(",", "."),
        zorder=2,
    )

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

    ax.legend(
        facecolor=_BG_COLOR,
        edgecolor=_GRID_COLOR,
        labelcolor=_TEXT_COLOR,
        fontsize=9,
    )

    plt.tight_layout(pad=1.5)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=_BG_COLOR)
    plt.close(fig)
    buf.seek(0)
    return buf


def _fmt_brl(value):
    """Format a float as Brazilian currency string: 3846.55 -> 'R$ 3.846,55'"""
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def send_discord_alert(product, analytics, seller_info):
    """
    Sends a Discord embed with an attached price history chart on every run.
    seller_info must include 'source_url' (the winning platform URL).
    """
    if not DISCORD_WEBHOOK_URL:
        print("[Notifier] DISCORD_WEBHOOK_URL not set. Skipping notification.")
        return

    current_price = analytics.get("current_cash_price") or 0.0
    low_7d = analytics.get("low_7d") or 0.0
    avg_7d = analytics.get("avg_7d") or 0.0
    max_threshold = product.get("max_alert_threshold", 3800.00)
    target_price = product.get("target_price", 0.0)

    seller_name = seller_info.get("seller_name") or "Desconhecido"
    is_fba = seller_info.get("is_fba", False)
    source_url = seller_info.get("source_url") or product.get("urls", [""])[0]
    source_platform = seller_info.get("source_platform", "").capitalize()
    delivery_label = "FBA ✅ (Enviado pela Amazon)" if is_fba else f"Direto — {source_platform}"

    if current_price <= max_threshold:
        embed_title = "🚨 ALERTA DE PREÇO - OPORTUNIDADE!"
        embed_color = 5763719  # Green
    else:
        embed_title = "📊 RELATÓRIO DE VARREDURA HOURLY"
        embed_color = 3447003  # Blue

    embed = {
        "title": embed_title,
        "color": embed_color,
        "url": source_url,
        "fields": [
            {
                "name": "📦 Produto",
                "value": product["name"],
                "inline": False,
            },
            {
                "name": "💰 Menor Preço Encontrado",
                "value": _fmt_brl(current_price),
                "inline": True,
            },
            {
                "name": "🚚 Vendedor & Plataforma",
                "value": f"{seller_name}\n{delivery_label}",
                "inline": True,
            },
            {
                "name": "\u200b",
                "value": "\u200b",
                "inline": False,
            },
            {
                "name": "📉 Menor Preço (7 Dias)",
                "value": _fmt_brl(low_7d),
                "inline": True,
            },
            {
                "name": "📊 Média de Preço (7 Dias)",
                "value": _fmt_brl(avg_7d),
                "inline": True,
            },
            {
                "name": "\u200b",
                "value": "\u200b",
                "inline": False,
            },
            {
                "name": "🎯 Preço Alvo",
                "value": _fmt_brl(target_price),
                "inline": True,
            },
            {
                "name": "🔴 Teto de Oportunidade",
                "value": _fmt_brl(max_threshold),
                "inline": True,
            },
        ],
        "image": {"url": "attachment://chart.png"},
        "footer": {
            "text": f"Price Tracker • {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M UTC')}"
        },
    }

    payload = {"embeds": [embed]}
    chart_buf = generate_price_chart(product["id"], product["name"], max_threshold)

    try:
        if chart_buf:
            files = {"file": ("chart.png", chart_buf, "image/png")}
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
