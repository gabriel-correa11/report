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

# Chart colours
_BG_COLOR      = "#2F3136"
_SURFACE_COLOR = "#36393F"
_TEXT_COLOR    = "#DCDDDE"
_GRID_COLOR    = "#40444B"
_LINE_COLOR    = "#5865F2"

# Embed colours (decimal)
_COLOR_GREEN = 0x2ECC71   # price <= target_price
_COLOR_BLUE  = 0x3498DB   # standard hourly report

# Rank badges — index 0 = cheapest
_RANK_BADGES = ["🟢", "🔵", "🟡", "🟠", "🔴"]

_PLATFORM_LABELS = {
    "amazon": "Amazon",
    "kabum":  "KaBuM!",
    "magalu": "Magazine Luiza",
}

# GitHub Actions bot icon used in the footer
_GH_ICON = "https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png"


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Chart generation
# ---------------------------------------------------------------------------

def generate_price_chart(product_id, product_name):
    """
    Queries 7-day price history and produces a dark-themed line chart.
    Returns io.BytesIO PNG buffer or None if there is insufficient data.
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
        color=_TEXT_COLOR, fontsize=12, pad=12,
    )

    plt.tight_layout(pad=1.5)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=_BG_COLOR)
    plt.close(fig)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_brl(value):
    """3846.55 -> 'R$ 3.846,55'"""
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _platform_label(platform):
    return _PLATFORM_LABELS.get(platform, platform.capitalize())


def _seller_label(candidate):
    """Returns 'Amazon' or 'Amazon (Over Power / FBA)' style string."""
    platform_lbl = _platform_label(candidate["platform"])
    seller = candidate.get("seller_name") or platform_lbl
    fba_tag = " / FBA" if candidate.get("is_fba") else ""
    # Skip redundant seller name when it matches the platform name
    if seller.lower().replace("!", "") == platform_lbl.lower().replace("!", ""):
        return f"{platform_lbl}{fba_tag}"
    return f"{platform_lbl} ({seller}{fba_tag})"


# ---------------------------------------------------------------------------
# Embed field builders
# ---------------------------------------------------------------------------

def _field_best_offer(best):
    """
    Field 1 (inline) — 🏆 Melhor Oferta Atual
    Shows store+seller, bold price, and a direct purchase link.
    """
    label     = _seller_label(best)
    price_str = _fmt_brl(best["cash_price"])
    url       = best.get("url", "")
    link      = f"[🛒 Comprar Agora]({url})" if url else ""

    value = f"**{label}**\n**{price_str}**"
    if link:
        value += f"\n{link}"

    return {"name": "🏆 Melhor Oferta Atual", "value": value, "inline": True}


def _field_savings(best, candidates, product):
    """
    Field 2 (inline) — 📉 Variação / Desconto
    Shows savings vs. the second-cheapest store (if available),
    otherwise shows Pix discount or comparison to max threshold.
    """
    best_price    = best["cash_price"]
    max_threshold = product.get("max_alert_threshold", 3800.00)
    lines = []

    # Savings vs next cheapest store
    others = [c for c in candidates if c["url"] != best["url"]]
    if others:
        next_price  = others[0]["cash_price"]
        next_label  = _platform_label(others[0]["platform"])
        saving      = next_price - best_price
        saving_pct  = (saving / next_price) * 100
        lines.append(
            f"**{_fmt_brl(saving)} mais barato** que {next_label}\n"
            f"({saving_pct:.1f}% de economia)".replace(".", ",")
        )

    # Savings vs max threshold
    if best_price < max_threshold:
        delta = max_threshold - best_price
        lines.append(f"📌 {_fmt_brl(delta)} abaixo do teto")

    value = "\n".join(lines) if lines else "Sem comparativo disponível."
    return {"name": "📉 Variação / Desconto", "value": value, "inline": True}


def _field_store_comparison(candidates):
    """
    Field 3 (full-width) — 🏪 Comparativo de Lojas Monitoradas
    Blockquote-style list of every scanned store with badge, bold price,
    and a Ver Oferta link.
    """
    lines = []
    for i, c in enumerate(candidates):
        badge     = _RANK_BADGES[i] if i < len(_RANK_BADGES) else "⚪"
        label     = _seller_label(c)
        price_str = _fmt_brl(c["cash_price"])
        url       = c.get("url", "")
        link      = f"[Ver Oferta]({url})" if url else ""

        delivery = "FBA" if c.get("is_fba") else "Direto"
        line = f"{badge} **{label}** ({delivery}) ➔ **{price_str}**"
        if link:
            line += f"  `{link}`"
        lines.append(line)

    value = "\n".join(lines) if lines else "Nenhuma loja monitorada retornou preços."
    return {"name": "🏪 Comparativo de Lojas Monitoradas", "value": value, "inline": False}


# ---------------------------------------------------------------------------
# Main notifier
# ---------------------------------------------------------------------------

def send_discord_alert(product, analytics, seller_info):
    """
    Sends a rich Discord embed with 3 structured fields and a historical
    price chart attached as image. Called on every hourly run.
    """
    if not DISCORD_WEBHOOK_URL:
        print("[Notifier] DISCORD_WEBHOOK_URL not set. Skipping notification.")
        return

    candidates   = seller_info.get("all_candidates", [])
    target_price = product.get("target_price", 0.0)
    best         = candidates[0] if candidates else None
    best_price   = best["cash_price"] if best else (analytics.get("current_cash_price") or 0.0)

    # Dynamic colour: green if best price is at or below target, blue otherwise
    embed_color = _COLOR_GREEN if best_price <= target_price else _COLOR_BLUE

    now_brt = datetime.now(timezone.utc)
    footer_text = (
        f"Última sincronização • Hoje às {now_brt.strftime('%H:%M')} UTC  •  "
        "github.com/gabriel-correa11/report"
    )

    fields = []
    if best:
        fields.append(_field_best_offer(best))
        fields.append(_field_savings(best, candidates, product))
        # Spacer so field 3 always starts on a new row
        fields.append({"name": "\u200b", "value": "\u200b", "inline": False})
    fields.append(_field_store_comparison(candidates))

    embed = {
        "title": f"🎮 MONITOR DE PREÇOS — {product['name']}",
        "color": embed_color,
        "fields": fields,
        "image": {"url": "attachment://chart.png"},
        "footer": {
            "text": footer_text,
            "icon_url": _GH_ICON,
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
