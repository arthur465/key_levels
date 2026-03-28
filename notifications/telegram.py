"""
notifications/telegram.py
Send messages to a Telegram chat via Bot API.
Uses aiohttp for async HTTP, with a sync wrapper for simplicity.
"""
import asyncio
import aiohttp
import config


async def _send_async(text: str, parse_mode: str = "Markdown") -> bool:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        print(f"[telegram] Credentials missing — would have sent:\n{text}\n")
        return False

    url     = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    config.TELEGRAM_CHAT_ID,
        "text":       text,
        "parse_mode": parse_mode,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return True
                body = await resp.text()
                print(f"[telegram] HTTP {resp.status}: {body}")
                return False
    except Exception as e:
        print(f"[telegram] Send error: {e}")
        return False


def send_message(text: str, parse_mode: str = "Markdown") -> bool:
    """Synchronous wrapper — safe to call from scheduler/main loop."""
    try:
        return asyncio.run(_send_async(text, parse_mode))
    except RuntimeError:
        # Already inside an event loop (e.g. Jupyter) — use create_task instead
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(_send_async(text, parse_mode))


def send_startup() -> None:
    msg = (
        "🚀 *BTC Liquidity System — ONLINE*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Symbol:  BTC/USDT Perpetual\n"
        f"Capital: ${config.INITIAL_CAPITAL:,} | Risk: {config.RISK_PER_TRADE*100:.0f}%/trade\n"
        f"TFs:     15m (day) | 1H + 4H (swing)\n"
        f"CVD:     {'ON' if config.USE_CVD else 'OFF'}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Monitoring PDH/PDL/PWH/PWL + session levels.\n"
        f"Alerts will fire when a valid OTE setup is detected."
    )
    send_message(msg)
