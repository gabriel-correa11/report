import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


def send_telegram_alert(product, analytics, seller_info):
    """
    Sends a Telegram notification for every run.
    Uses dynamic header to distinguish deal alerts from regular hourly reports.
    """
    current_price = analytics.get("current_cash_price") or 0.0
    low_7d = analytics.get("low_7d") or 0.0
    avg_7d = analytics.get("avg_7d") or 0.0

    max_threshold = product.get("max_alert_threshold", 3700.00)

    if current_price <= max_threshold:
        header_text = "🚨 *ALERTA DE PREÇO - OPORTUNIDADE!*"
    else:
        header_text = "📊 *RELATÓRIO DE VARREDURA HOURLY*"

    seller_name = seller_info.get("seller_name") or "Desconhecido"
    is_fba = seller_info.get("is_fba", False)
    fba_status = "FBA ✅" if is_fba else "Terceiro"

    message = (
        f"{header_text}\n\n"
        f"📦 *Produto:* {product['name']}\n"
        f"💰 *Preço à Vista:* R$ {current_price:.2f}\n"
        f"🚚 *Vendedor:* {seller_name} ({fba_status})\n\n"
        f"📊 *MÉTRICAS (7 DIAS):*\n"
        f"• Menor Preço (7d): R$ {low_7d:.2f}\n"
        f"• Média (7d): R$ {avg_7d:.2f}\n"
        f"• Preço Alvo: R$ {product['target_price']:.2f}\n"
        f"• Teto de Oportunidade: R$ {max_threshold:.2f}\n\n"
        f"🔗 [Comprar Agora]({product['url']})"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "parse_mode": "Markdown",
        "text": message,
    }

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        response = requests.post(url, json=payload, timeout=15)
        response.raise_for_status()
        print(f"[Notifier] Telegram alert sent for product_id={product['id']}")
    except requests.RequestException as e:
        print(f"[Notifier] Failed to send Telegram alert for product_id={product['id']}: {e}")
