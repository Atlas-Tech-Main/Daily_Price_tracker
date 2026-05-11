import os
import logging
import datetime
import pytz
import requests
from pymongo import MongoClient
from dotenv import load_dotenv

_ENV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
)
load_dotenv(_ENV_PATH)

logging.basicConfig(level=logging.INFO)

MONGO_URI = os.environ.get("MONGO_URI")
AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID")
AZURE_CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET")
AZURE_TENANT_ID = os.environ.get("AZURE_TENANT_ID")
OUTLOOK_EMAIL = os.environ.get("OUTLOOK_EMAIL")

TIMEZONE = "Asia/Kolkata"

def get_graph_token():
    url = f"https://login.microsoftonline.com/{AZURE_TENANT_ID}/oauth2/v2.0/token"
    data = {
        "client_id": AZURE_CLIENT_ID,
        "scope": "https://graph.microsoft.com/.default",
        "client_secret": AZURE_CLIENT_SECRET,
        "grant_type": "client_credentials"
    }
    response = requests.post(url, data=data)
    response.raise_for_status()
    return response.json()["access_token"]

def send_birthday_email(admin_emails, upcoming_birthdays):
    token = get_graph_token()
    url = f"https://graph.microsoft.com/v1.0/users/{OUTLOOK_EMAIL}/sendMail"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    birthday_list_html = "".join([
        f"<li><strong>{emp.get('name', 'Unknown')}</strong> ({emp.get('email') or 'No email'})</li>"
        for emp in upcoming_birthdays
    ])

    email_content = f"""
        <div style="font-family: Aptos, Arial, sans-serif; color: #333; line-height: 1.6;">
        <h3 style="color: #314568;">Upcoming Employee Birthdays</h3>
        <p>The following employees have birthdays tomorrow:</p>
        <ul>
            {birthday_list_html}
        </ul>
        <p>Please make sure to wish them!</p>
        </div>
    """

    to_recipients = [{"emailAddress": {"address": email}} for email in admin_emails]

    message_payload = {
        "message": {
            "subject": "Upcoming Employee Birthdays - Reminder",
            "body": {
                "contentType": "HTML",
                "content": email_content
            },
            "toRecipients": to_recipients
        },
        "saveToSentItems": "true"
    }

    response = requests.post(url, headers=headers, json=message_payload)
    if response.status_code in (200, 202):
        logging.info("Sent birthday reminder emails to admins via Graph API.")
    else:
        logging.error(f"Failed to send email: {response.status_code} {response.text}")

def run_birthday_alert():
    try:
        if not MONGO_URI:
            logging.error("MONGO_URI environment variable not found.")
            return {"status": "error", "message": "MONGO_URI not found."}

        client = MongoClient(MONGO_URI)
        db = client.get_database() # Uses the database specified in URI

        tz = pytz.timezone(TIMEZONE)
        now = datetime.datetime.now(tz)
        tomorrow = now + datetime.timedelta(days=1)
        target_month = tomorrow.month
        target_day = tomorrow.day

        query = {
            "$expr": {
                "$and": [
                    { "$eq": [{ "$month": "$dob" }, target_month] },
                    { "$eq": [{ "$dayOfMonth": "$dob" }, target_day] }
                ]
            }
        }

        upcoming_birthdays = list(db.employees.find(query))

        if upcoming_birthdays:
            logging.info(f"Found {len(upcoming_birthdays)} upcoming birthdays.")
            
            admin_config = db.config.find_one({"key": "admin_emails"})
            admin_emails = admin_config.get("list", []) if admin_config else []

            if admin_emails:
                send_birthday_email(admin_emails, upcoming_birthdays)
            else:
                logging.info("No admin emails configured. Cannot send birthday reminder.")
        else:
            logging.info("No birthdays tomorrow.")

        return {
            "status": "ok",
            "upcoming_birthdays_count": len(upcoming_birthdays),
            "timestamp": now.isoformat()
        }

    except Exception as e:
        logging.error(f"Error in birthday alert job: {e}")
        return {
            "status": "error",
            "message": str(e)
        }

if __name__ == "__main__":
    run_birthday_alert()
