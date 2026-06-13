"""
hourly_alert.py
---------------
Standalone serverless handler (Vercel /api/hourly_alert) that:
  - Runs every hour via an external cron (e.g. cron-job.org / Vercel Cron)
  - For each stock, compares the *current* intraday price against the
    *previous trading day's closing price*
  - Sends ONE batched alert email per hourly run when BOTH conditions are met:
      1. |price change vs prev close| >= HOURLY_PRICE_CHANGE_THRESHOLD (5%)
      2. Cumulative intraday volume > threshold (100k if market cap >= 1000cr, else 20k)
  - All qualifying stocks are collected first, then a single SMTP call sends
    one email to all recipients — no duplicate emails within the same run.

The existing index.py interval-reporting pipeline is left completely untouched.
"""


import yfinance as yf
from concurrent.futures import ThreadPoolExecutor
import logging
import datetime
import pytz
import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from http.server import BaseHTTPRequestHandler
from dotenv import load_dotenv
import json
import requests
from stocks_manager import (
    load_indian_symbols,
    load_global_symbols,
    load_indian_stock_emails,
    load_global_stock_emails
)

# ── Load env vars ──────────────────────────────────────────────────────────────
_ENV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
)
load_dotenv(_ENV_PATH)

logging.basicConfig(level=logging.INFO)

# Global variable for best-effort cooldown in serverless environment
_LAST_EMAIL_SENT_TIME = {}


# ==========================================
# CONFIGURATION
# ==========================================
INDEX_SYMBOLS = []

TIMEZONE = "Asia/Kolkata"

# Thresholds
HOURLY_PRICE_CHANGE_THRESHOLD = 5.0    # percent — alert if |change vs prev close| >= this
# Volume threshold is dynamically determined based on market cap:
# >= 1000 Cr: 100,000
# < 1000 Cr: 20,000

# SMTP / email settings (from .env)
SMTP_SERVER   = os.environ.get("SMTP_SERVER",   "smtp.gmail.com")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", 587))
SMTP_EMAIL    = os.environ.get("SMTP_EMAIL",    "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
# TO_EMAILS has been replaced with dynamically loaded lists inside run_hourly_alert


# ==========================================
# DATA FETCHING
# ==========================================

def _get_prev_close(ticker, today_date) -> float | None:
    """
    Returns the closing price of the most recent trading day BEFORE today.
    Looks back up to 10 days to handle long weekends / holidays.
    Falls back to ticker.info['previousClose'] if history is unavailable.
    """
    try:
        df = ticker.history(period="10d", interval="1d")
        if df.empty:
            return ticker.info.get("previousClose")
        df_prev = df[df.index.date < today_date]
        if df_prev.empty:
            return ticker.info.get("previousClose")
        return float(df_prev.iloc[-1]["Close"])
    except Exception:
        try:
            return ticker.info.get("previousClose")
        except Exception:
            return None


def _fetch_symbol(symbol: str, today_date) -> dict | None:
    """
    Fetches intraday data for `symbol` and returns a dict with:
      symbol, current_price, prev_close, pct_change, volume
    Returns None if data is insufficient to evaluate.
    """
    try:
        ticker = yf.Ticker(symbol)

        # ── Intraday 1-minute bars ─────────────────────────────────────────────
        df = ticker.history(period="1d", interval="1m")
        if df.empty:
            return None

        df_today = df[df.index.date == today_date]
        if df_today.empty:
            return None  # Not traded today — nothing to alert on

        current_price = float(df_today.iloc[-1]["Close"])
        cum_volume    = int(df_today["Volume"].sum())

        # ── Previous day's close ───────────────────────────────────────────────
        prev_close = _get_prev_close(ticker, today_date)
        if prev_close is None or prev_close == 0:
            return None

        pct_change = ((current_price - prev_close) / prev_close) * 100
        market_cap = ticker.info.get("marketCap", 0)

        return {
            "symbol":        symbol,
            "current_price": current_price,
            "prev_close":    prev_close,
            "pct_change":    pct_change,
            "volume":        cum_volume,
            "market_cap":    market_cap,
        }
    except Exception as e:
        logging.warning(f"[hourly_alert] Error fetching {symbol}: {e}")
        return None


def fetch_all(today_date, indian_symbols: list[str], global_symbols: list[str]) -> list[dict]:
    """Fetches data for all symbols in parallel and returns raw results."""
    all_symbols = indian_symbols + global_symbols + INDEX_SYMBOLS
    results = []
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {
            executor.submit(_fetch_symbol, sym, today_date): sym
            for sym in all_symbols
        }
        for future in futures:
            data = future.result()
            if data is not None:
                results.append(data)
    return results


# ==========================================
# ALERT EVALUATION
# ==========================================
def evaluate_alerts(all_data: list[dict], today_date: datetime.date) -> list[dict]:
    """
    Filters stocks that breach BOTH thresholds.
    Returns list of alert dicts ready for the email.
    """
    alerts = []
    date_str = today_date.isoformat()

    for row in all_data:
        sym = row["symbol"]
        market_cap = row.get("market_cap", 0)
        # 1000 Cr = 10,000,000,000
        vol_thresh = 100000 if market_cap >= 10_000_000_000 else 20000

        price_ok  = abs(row["pct_change"]) >= HOURLY_PRICE_CHANGE_THRESHOLD
        volume_ok = row["volume"] >= vol_thresh
        if price_ok or volume_ok:
            row["vol_thresh"] = vol_thresh
            alerts.append(row)
            logging.info(
                f"[hourly_alert] ALERT: {sym} | "
                f"Prev close ₹{row['prev_close']:.2f} → "
                f"Current ₹{row['current_price']:.2f} "
                f"({row['pct_change']:+.2f}%) | Vol {row['volume']:,} | Thresh {vol_thresh:,}"
            )

    return alerts


# ==========================================
# EMAIL
# ==========================================
def send_hourly_alert_email(alerts: list[dict], now_dt: datetime.datetime, to_emails: list[str], alert_type: str) -> bool:
    """
    Sends ONE HTML alert email to ALL configured recipients for the given alert type.
    Each recipient receives the same message (BCC-style via sendmail list).
    Returns True on success, False on failure.
    """
    global _LAST_EMAIL_SENT_TIME
    
    last_sent = _LAST_EMAIL_SENT_TIME.get(alert_type)
    if last_sent is not None:
        elapsed = now_dt - last_sent
        if elapsed < datetime.timedelta(minutes=1):
            logging.info(f"[hourly_alert] Cooldown active (1 min) for {alert_type}. Skipping email.")
            return False

    if not SMTP_EMAIL or not SMTP_PASSWORD:
        logging.warning("[hourly_alert] SMTP credentials missing — skipping email.")
        return False
    if not to_emails:
        logging.warning(f"[hourly_alert] No recipients configured for {alert_type} alerts — skipping email.")
        return False

    date_str = now_dt.strftime("%Y-%m-%d")
    time_str = now_dt.strftime("%H:%M")

    # ── Build HTML rows ────────────────────────────────────────────────────────
    rows_html = ""
    for a in sorted(alerts, key=lambda x: abs(x["pct_change"]), reverse=True):
        direction = "▲" if a["pct_change"] > 0 else "▼"
        color     = "#27ae60" if a["pct_change"] > 0 else "#e74c3c"
        rows_html += f"""
        <tr>
          <td style='text-align:left;font-weight:bold;color:#314568;padding:9px 14px;border-bottom:1px solid #D1DCE2;font-family:"Montserrat",sans-serif;'>{a['symbol']}</td>
          <td style='text-align:right;color:#0D1B2A;padding:9px 14px;border-bottom:1px solid #D1DCE2;font-family:"Montserrat",sans-serif;'>&#8377;{a['prev_close']:.2f}</td>
          <td style='text-align:right;color:#0D1B2A;padding:9px 14px;border-bottom:1px solid #D1DCE2;font-family:"Montserrat",sans-serif;'>&#8377;{a['current_price']:.2f}</td>
          <td style='text-align:right;color:#0D1B2A;padding:9px 14px;border-bottom:1px solid #D1DCE2;font-family:"Montserrat",sans-serif;'>{a['volume']:,}</td>
          <td style='text-align:right;font-weight:bold;color:{color};padding:9px 14px;border-bottom:1px solid #D1DCE2;font-family:"Montserrat",sans-serif;'>{direction} {abs(a['pct_change']):.2f}%</td>
        </tr>"""

    # Format header text and count label based on alert_type
    header_title = f"&#9200; Intraday {alert_type} Alert &mdash; {date_str} @ {time_str}"
    item_label = "stock(s)" if alert_type == "Stock" else "index(es)"

    html = f"""
    <html><head><meta charset='UTF-8'>
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
    </head>
    <body style='margin:0;padding:0;background:#F6F1E9;font-family:"minion Variable concept", "Montserrat", sans-serif;'>
      <table width='100%' cellpadding='0' cellspacing='0' style='background:#F6F1E9;padding:30px 0'>
        <tr><td align='center'>
          <table width='680' cellpadding='0' cellspacing='0'
                 style='background:#ffffff;border-radius:8px;overflow:hidden;
                        box-shadow:0 2px 10px rgba(0,0,0,0.10)'>
            <!-- Header -->
            <tr>
              <td style='background:#ffffff;padding:24px 32px;border-bottom:1px solid #D1DCE2;'>
                <table width='100%' cellpadding='0' cellspacing='0'>
                  <tr>
                    <td width='80' style='vertical-align:middle;'>
                      <img src="https://atlascapital.in/wp-content/uploads/2025/02/Logo-blue-.png" alt="Atlas Capital" style="max-height: 60px;" />
                    </td>
                    <td style='vertical-align:middle;text-align:left;padding-left:100px;'>
                      <h2 style='margin:0;color:#314568;font-size:15px;font-family:"Montserrat",sans-serif;'>{header_title}</h2>
                      <p style='margin:6px 0 0;color:#607CA4;font-size:10px;font-family:"Montserrat",sans-serif;'>
                        {len(alerts)} {item_label} moved &ge;{HOURLY_PRICE_CHANGE_THRESHOLD}% from previous day&rsquo;s close
                        with volume exceeding their respective thresholds
                      </p>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
            <!-- Table -->
            <tr>
              <td style='padding:24px 32px'>
                <table width='100%' cellpadding='0' cellspacing='0'
                       style='border-collapse:collapse;font-size:14px'>
                  <thead>
                    <tr style='background:#0D1B2A'>
                      <th style='text-align:left;padding:10px 14px;color:#F6F1E9;font-weight:700;border-bottom:2px solid #314568;font-family:"Montserrat",sans-serif;'>Symbol</th>
                      <th style='text-align:right;padding:10px 14px;color:#F6F1E9;font-weight:700;border-bottom:2px solid #314568;font-family:"Montserrat",sans-serif;'>Prev Close</th>
                      <th style='text-align:right;padding:10px 14px;color:#F6F1E9;font-weight:700;border-bottom:2px solid #314568;font-family:"Montserrat",sans-serif;'>Current Price</th>
                      <th style='text-align:right;padding:10px 14px;color:#F6F1E9;font-weight:700;border-bottom:2px solid #314568;font-family:"Montserrat",sans-serif;'>Volume</th>
                      <th style='text-align:right;padding:10px 14px;color:#F6F1E9;font-weight:700;border-bottom:2px solid #314568;font-family:"Montserrat",sans-serif;'>Change</th>
                    </tr>
                  </thead>
                  <tbody>{rows_html}</tbody>
                </table>
              </td>
            </tr>
            <!-- Footer -->
            <tr>
              <td style='background:#0D1B2A;padding:16px 32px;text-align:center;
                         color:#C6A962;font-size:12px;border-top:1px solid #314568;font-family:"Montserrat",sans-serif;'>
                Atlas Capital Automation &bull; Intraday Alert System
              </td>
            </tr>
          </table>
        </td></tr>
      </table>
    </body></html>
    """

    try:
        msg = MIMEMultipart("related")
        msg["Subject"] = (
            f"\U0001f514 Intraday {alert_type} Alert: {len(alerts)} {item_label} moved "
            f">{HOURLY_PRICE_CHANGE_THRESHOLD}% from prev close | {date_str} {time_str}"
        )
        msg["From"] = SMTP_EMAIL
        msg["To"]   = ", ".join(to_emails)

        msg_alt = MIMEMultipart("alternative")
        msg.attach(msg_alt)
        msg_alt.attach(MIMEText(html, "html"))

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_EMAIL, SMTP_PASSWORD)
        server.sendmail(SMTP_EMAIL, to_emails, msg.as_string())
        server.quit()

        _LAST_EMAIL_SENT_TIME[alert_type] = now_dt

        logging.info(
            f"[hourly_alert] {alert_type} email sent to {len(to_emails)} recipient(s) "
            f"for {len(alerts)} {item_label}."
        )
        return True
    except Exception as e:
        logging.error(f"[hourly_alert] Failed to send {alert_type} email: {e}")
        return False


def run_hourly_alert():
    tz      = pytz.timezone(TIMEZONE)
    now     = datetime.datetime.now(tz)
    today   = now.date()
    date_str = today.isoformat()

    logging.info(f"[hourly_alert] Triggered at {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    # ── Fetch all symbols ──────────────────────────────────────────────────
    indian_symbols = load_indian_symbols()
    global_symbols = load_global_symbols()
    all_data = fetch_all(today, indian_symbols, global_symbols)
    logging.info(f"[hourly_alert] Fetched data for {len(all_data)} symbol(s).")

    # ── Evaluate which symbols breach thresholds ──────────────────────
    alerts = evaluate_alerts(all_data, today)

    # ── Separate stock alerts ────────────────────────────────────
    indian_alerts = [a for a in alerts if a["symbol"] in indian_symbols]
    global_alerts = [a for a in alerts if a["symbol"] in global_symbols]

    indian_email_sent = False
    if indian_alerts:
        to_emails = load_indian_stock_emails()
        indian_email_sent = send_hourly_alert_email(indian_alerts, now, to_emails, "Indian Stock")

    global_email_sent = False
    if global_alerts:
        to_emails = load_global_stock_emails()
        global_email_sent = send_hourly_alert_email(global_alerts, now, to_emails, "Global Stock")

    response = {
        "status":          "ok",
        "timestamp":       now.isoformat(),
        "symbols_checked": len(all_data),
        "alerts_fired":    len(alerts),
        "indian_alerts":   len(indian_alerts),
        "global_alerts":   len(global_alerts),
        "indian_email_sent": indian_email_sent,
        "global_email_sent": global_email_sent,
    }
    return response

