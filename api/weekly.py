from http.server import BaseHTTPRequestHandler
import requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
import os
import json
from dotenv import load_dotenv
import yfinance as yf
import pandas as pd

_ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(_ENV_PATH)

from stocks_manager import load_symbols, load_weekly_emails



# Email Configuration - Set these in Vercel Environment Variables
SMTP_SERVER   = os.environ.get("SMTP_SERVER",   "smtp.gmail.com")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", 587))
SMTP_EMAIL    = os.environ.get("SMTP_EMAIL",    "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
# TO_EMAILS has been replaced with dynamically loaded lists inside send_email
# ==========================================
# WEEKLY RETURN LOGIC  (via Yahoo Finance)
# ==========================================
def get_target_dates():
    today = datetime.today()
    current_date = today.date()
    
    # Calculate days back to the most recent Friday
    days_since_friday = (today.weekday() - 4) % 7
    # If today is Friday, we want the previous Friday (7 days ago)
    days_to_last_friday = 7 if days_since_friday == 0 else days_since_friday
    
    last_date = current_date - timedelta(days=days_to_last_friday)
    return current_date, last_date

def get_closest_close(hist, target_date):
    # Convert dates to match tz-aware hist index
    if hist.index.tz is not None:
        target_dt = pd.to_datetime(target_date).tz_localize(hist.index.tz)
    else:
        target_dt = pd.to_datetime(target_date)
    
    past_dates = hist[hist.index <= target_dt]
    if past_dates.empty:
        return None, None
    closest_date = past_dates.index[-1]
    return closest_date.date(), float(past_dates.loc[closest_date, "Close"])

def calculate_single_return(symbol):
    try:
        current_date, last_date = get_target_dates()
        ticker = yf.Ticker(symbol)
        
        # Using 1 month to ensure we get data for the last two dates
        try:
            hist = ticker.history(period="1mo")
        except Exception:
            import pandas as pd
            hist = pd.DataFrame()

        # FALLBACK: Yahoo Finance often returns only 1 day of data for "-SM" symbols.
        # We try to strip "-SM" and fetch the regular symbol which usually has the full history.
        if len(hist) < 2 and "-SM" in symbol:
            alt_symbol = symbol.replace("-SM", "")
            alt_ticker = yf.Ticker(alt_symbol)
            try:
                alt_hist = alt_ticker.history(period="1mo")
                if len(alt_hist) >= 2:
                    hist = alt_hist
                    ticker = alt_ticker
            except Exception:
                pass

        c_date, close_current = get_closest_close(hist, current_date)
        l_date, close_last = get_closest_close(hist, last_date)

        if close_current is None:
            try:
                fallback_close = ticker.info.get("previousClose")
                if fallback_close is not None:
                    close_current = fallback_close
                    c_date = current_date
            except Exception:
                pass

        if close_current is None or close_last is None:
            return {"ticker": symbol, "error": "Missing price data for the target dates"}
            
        weekly_return = ((close_current - close_last) / close_last) * 100
        return {
            "ticker": symbol,
            "last_close":   close_last,
            "last_date":    str(l_date),
            "current_close": close_current,
            "current_date":  str(c_date),
            "weekly_return":       weekly_return
        }
    except Exception as e:
        return {"ticker": symbol, "error": str(e)}

def get_all_weekly_returns():
    company_symbols = load_symbols()
    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(calculate_single_return, sym): sym for sym in company_symbols}
        for future in futures:
            results.append(future.result())
    return results

# ==========================================
# EMAIL LOGIC
# ==========================================
def format_html_email(results):
    current_date, _ = get_target_dates()
    html = f"""
    <html>
      <head>
        <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
        <style>
          body {{ font-family: "minion Variable concept", "Montserrat", sans-serif; background-color: #F6F1E9; margin: 0; padding: 20px; }}
          .container {{ background-color: #ffffff; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); max-width: 800px; margin: 0 auto; overflow: hidden; }}

          .content {{ padding: 24px 32px; }}
          table {{ border-collapse: collapse; width: 100%; font-family: "Montserrat", sans-serif; font-size: 14px; }}
          th, td {{ border-bottom: 1px solid #D1DCE2; text-align: right; padding: 12px 14px; color: #0D1B2A; }}
          th {{ background-color: #0D1B2A; color: #F6F1E9; text-align: center; font-weight: bold; border-bottom: 2px solid #314568; }}
          .positive {{ color: #27ae60; font-weight: bold; }}
          .negative {{ color: #e74c3c; font-weight: bold; }}
          .ticker {{ text-align: left; font-weight: bold; color: #314568; }}
          .footer {{ background-color: #0D1B2A; padding: 16px 32px; text-align: center; color: #C6A962; font-size: 12px; font-family: "Montserrat", sans-serif; border-top: 1px solid #314568; }}
        </style>
      </head>
      <body>
        <div class="container">
          <table cellpadding="0" cellspacing="0" style="width: 100%; background-color: #ffffff; border-bottom: 1px solid #D1DCE2; font-family: 'Montserrat', sans-serif;">
            <tr>
              <td style="border-bottom: none; text-align: left; padding: 24px 0 24px 32px; width: 80px; vertical-align: middle;">
                <img src="cid:logo" alt="Atlas Capital" style="max-height: 60px;" />
              </td>
              <td style="border-bottom: none; text-align: left; padding: 24px 32px 24px 100px; vertical-align: middle;">
                <h2 style="margin: 0; color: #314568; font-size: 15px;">Weekly Return Report</h2>
                <p style="margin: 6px 0 0; color: #607CA4; font-size: 10px;">Week Ending {current_date}</p>
              </td>
            </tr>
          </table>
          <div class="content">
            <table>
              <tr>
                <th style="text-align: left;">Ticker</th>
                <th>Previous Close</th>
                <th>Current Close</th>
                <th>Weekly Return (%)</th>
              </tr>
    """
    
    for res in results:
        if "error" in res:
            html += f"<tr><td class='ticker'>{res['ticker']}</td><td colspan='3' style='text-align: center;'>Error: {res['error']}</td></tr>\n"
        else:
            ret = res['weekly_return']
            color_class = "positive" if ret >= 0 else "negative"
            html += f"""
              <tr>
                <td class='ticker'>{res['ticker']}</td>
                <td>{res['last_close']:.2f} <br><small style="color: #607CA4;">({res['last_date']})</small></td>
                <td>{res['current_close']:.2f} <br><small style="color: #607CA4;">({res['current_date']})</small></td>
                <td class='{color_class}'>{ret:.2f}%</td>
              </tr>
            """
            
    html += """
            </table>
          </div>
          <div class="footer">
            Atlas Capital Automation &bull; Weekly Report System
          </div>
        </div>
      </body>
    </html>
    """
    return html

def send_email(html_content):
    if not SMTP_EMAIL or not SMTP_PASSWORD:
        print("Email configuration missing. Skipping email send.")
        return False, "SMTP variables not set"
        
    to_emails = load_weekly_emails()
    if not to_emails:
        print("No recipient emails configured.")
        return False, "TO_EMAIL variable not set or invalid"
        
    try:
        msg = MIMEMultipart("related")
        msg["Subject"] = f"Weekly Stock Returns - {datetime.today().date()}"
        msg["From"] = SMTP_EMAIL
        msg["To"] = ", ".join(to_emails)

        msg_alt = MIMEMultipart("alternative")
        msg.attach(msg_alt)
        msg_alt.attach(MIMEText(html_content, "html"))

        try:
            logo_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logo.png")
            with open(logo_path, "rb") as f:
                img_data = f.read()
            # Do NOT pass name="logo.png", to prevent it appearing as an attachment
            image = MIMEImage(img_data)
            image.add_header('Content-ID', '<logo>')
            # Do NOT pass filename="logo.png" here either
            image.add_header('Content-Disposition', 'inline')
            msg.attach(image)
        except Exception as e:
            print(f"Could not attach logo: {e}")

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_EMAIL, SMTP_PASSWORD)
        server.sendmail(SMTP_EMAIL, to_emails, msg.as_string())
        server.quit()
        return True, "Email sent successfully"
    except Exception as e:
        print(f"Error sending email: {e}")
        return False, str(e)

def run_weekly_report():
    try:
        print("Fetching weekly returns...")
        results = get_all_weekly_returns()
        
        print("Formatting email...")
        html_content = format_html_email(results)
        
        print("Sending email...")
        email_success, email_msg = send_email(html_content)
        
        response_data = {
            "status": "success",
            "symbols_processed": len(results),
            "email_sent": email_success,
            "email_message": email_msg
        }
        return response_data
        
    except Exception as e:
        raise e
if __name__=="__main__":
    run_weekly_report()