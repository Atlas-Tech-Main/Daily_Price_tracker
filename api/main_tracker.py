import yfinance as yf
from concurrent.futures import ThreadPoolExecutor
import datetime
import pytz
import requests
import json
import logging
import os
from dotenv import load_dotenv

# Load .env from the project root (one level above this api/ folder)
_ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(_ENV_PATH)

from stocks_manager import load_indian_symbols, load_global_symbols

# ==========================================
# CONFIGURATION
# ==========================================
INDEX_SYMBOLS = ["^NSEI", "NIFTY_SME_EMERGE.NS"]

# PASTE YOUR APPS SCRIPT WEB APP URL HERE:
APPS_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbyhdbIDYjOOJab7OibhytbpM0ys0aINPRjXo4o0j3_sQDCo9wh4rhTzdZdM-c9KX0db/exec"

FETCH_TIMES = ["09:30", "11:00", "12:30", "14:30", "15:00"]
TIMEZONE = "Asia/Kolkata"

def get_last_traded_price(ticker, today_date):
    try:
        df_hist = ticker.history(period="5d", interval="1d")
        if df_hist.empty:
            return ticker.info.get("previousClose"), False

        df_prev = df_hist[df_hist.index.date < today_date]
        if df_prev.empty:
            df_prev = df_hist
            if df_prev.empty:
                return ticker.info.get("previousClose"), False

        last_close = float(df_prev.iloc[-1]['Close'])
        return last_close, False
    except Exception:
        try:
            return ticker.info.get("previousClose"), False
        except Exception:
            return None, False

def fetch_single(symbol, target_slot, now_dt):
    try:
        ticker = yf.Ticker(symbol)
        today_date = now_dt.date()

        df = ticker.history(period="1d", interval="1m")

        if df.empty:
            last_price, _ = get_last_traded_price(ticker, today_date)
            if last_price is None:
                return {"symbol": symbol, "error": "No data available"}
            return {"symbol": symbol, "price": last_price, "volume": 0}

        df_today = df[df.index.date == today_date]

        if df_today.empty:
            last_price, _ = get_last_traded_price(ticker, today_date)
            if last_price is None:
                last_price = float(df.iloc[-1]['Close'])
            return {"symbol": symbol, "price": last_price, "volume": 0}

        latest_price = float(df_today.iloc[-1]['Close'])
        cum_vol = int(df_today['Volume'].sum())

        if cum_vol == 0:
            hist_price, _ = get_last_traded_price(ticker, today_date)
            if hist_price is not None:
                latest_price = hist_price
            return {"symbol": symbol, "price": latest_price, "volume": 0}

        return {
            "symbol": symbol,
            "price": latest_price,
            "volume": cum_vol
        }
    except Exception as e:
        return {"symbol": symbol, "error": str(e)}

def fetch_all_data(target_slot, now_dt, all_symbols):
    results = {}
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(fetch_single, sym, target_slot, now_dt): sym for sym in all_symbols}
        for future in futures:
            data = future.result()
            if "error" not in data:
                results[data["symbol"]] = data
    return results

def get_target_slot(now):
    time_str = now.strftime("%H:%M")
    if time_str in FETCH_TIMES:
        return time_str
             
    now_minutes = now.hour * 60 + now.minute
    closest_slot = None
    min_diff = float('inf')
    
    for ft in FETCH_TIMES:
        parts = ft.split(":")
        ft_minutes = int(parts[0]) * 60 + int(parts[1])
        diff = abs(now_minutes - ft_minutes)
        if diff < min_diff:
            min_diff = diff
            closest_slot = ft
            
    return closest_slot


def run_main_tracker():
    tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(tz)
    
    company_symbols = load_indian_symbols() + load_global_symbols()
    all_symbols = company_symbols + INDEX_SYMBOLS

    target_slot = get_target_slot(now)
    date_str = now.strftime("%Y-%m-%d")
    
    if not target_slot:
        return 200, 'text/plain', b"Tracker running successfully, but current time does not match a schedule window. No fetch performed."

    market_data = fetch_all_data(target_slot, now, all_symbols)
    if not market_data:
        return 500, 'text/plain', b"Failed to fetch market data from Yahoo Finance."

    payload = {
        "date": date_str,
        "time_slot": target_slot,
        "companies": all_symbols,
        "fetch_times": FETCH_TIMES,
        "data": market_data
    }
    print(payload)
    try:
        resp = requests.post(APPS_SCRIPT_URL, json=payload, headers={"Content-Type": "application/json"})
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        return 500, 'text/plain', f"Error communicating with Google Sheets Apps Script: {str(e)}".encode('utf-8')
        
    response_data = {
        "status": "success",
        "time_slot": target_slot,
        "symbols_fetched": len(market_data),
    }
    return 200, 'application/json', json.dumps(response_data).encode('utf-8')
