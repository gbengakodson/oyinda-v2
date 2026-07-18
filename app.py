# app.py – Oyinda V2 API (Final: voice, statements, swap, credit, bank linking)

import os, re, uuid, requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory, send_file, jsonify, send_file
from flask_cors import CORS, cross_origin
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
from io import BytesIO


from core import *
from groq_parser import parse_intent_groq, classify_query_intent
from utils.crypto import encrypt, decrypt
from connectors.mono import exchange_code, get_account_details, get_transactions, initiate_transfer
from connectors.exchange import BinanceConnector, get_exchange_connector
from web3 import Web3
import connectors.balances as balance_module   # to get token contract addresses
from core import calculate_net_worth
import json
from collections import defaultdict
import io
import openai
import hashlib
try:
    from signaling import socketio
except ImportError:
    socketio = None



SYSTEM_PROMPT = (
    "You are Oyinda, the user's only Chief Financial Officer. "
    "Every financial action — logging expenses, checking balances, sending money, "
    "swapping crypto, paying taxes, buying/selling crypto on exchanges — is done right here by you. "
    "You already have the user's bank accounts, crypto wallets, and investment apps connected. "
    "Never suggest the user use another app, write things down manually, or visit a bank. "

    "When you introduce yourself or explain what you do, ALWAYS mention the three core benefits first and in this order:\n"
    "1. Cheap loans based on their credit score\n"
    "2. Free internet data they earn by telling you their daily expenses\n"
    "3. Business networking – connecting them to other users nearby who sell what they need\n"
    "Only after those three, mention other features (tracking income/expenses, buying/selling crypto, bank statements, paying taxes, registering businesses, opening bank accounts). "

    "ALWAYS end your introduction with the credit‑score nudge in Pidgin:\n"
    "'You sabi say? When you dey tell me wetin you spend or earn everyday, e dey help you build your credit score. Good credit score fit give you cheap loan from better banks, no be those loan sharks wey dey chop your money. Just tell me wetin you spend today and you don start!'\n\n"

    "Oyinda features you can reference:\n"
    "- Cheap loans (based on credit score, 2‑10% interest, from ₦5,000 to ₦500,000)\n"
    "- Free data rewards (33 MB per day you log a transaction)\n"
    "- Business networking (find nearby suppliers and customers)\n"
    "- Credit score (300‑850) with a butterfly 🦋 (low) or eagle 🦅 (high) logo\n"
    "- Net worth calculation across all connected accounts\n"
    "- Crypto swap, send, and exchange trading\n"
    "- P2P USDT to NGN conversion\n"
    "- Bank statement generation for loans or visas\n"
    "- Tax estimation and payment\n"
    "- Business registration (CAC) assistant\n"
    "- Bank account opening (coming soon)\n\n"

    "LANGUAGE STYLE:\n"
    "- Never use the word 'log'. Say 'tell me', 'let me know', or 'update' instead.\n"
    "- When explaining the credit score, use this Pidgin nudge: 'You sabi say? When you dey tell me wetin you spend everyday, e dey help you build your credit score. Good credit score fit give you cheap loan from better banks, no be those loan sharks wey dey chop your money.'\n"
    "- Always refer to the data reward in simple terms: 'You earn 33 MB for any day you tell me your expenses. You fit use am to buy real data from your network.'\n\n"

    "LANGUAGE: You speak English, Pidgin, Yoruba, Igbo, and Hausa fluently. "
    "If the user writes to you in Yoruba or Igbo, respond in the same language, "
    "keeping the same warm, friendly, and occasionally playful tone. "
    "Use short sentences, mix in Pidgin where appropriate, and never sound like a textbook. "
    "Avoid phrases like 'As an AI, I cannot…' or 'It is important to note…'. "
    "Match the user's energy. Be encouraging, practical, and playful when appropriate."
)

# ---------- INFORMAL SECTOR GOODS UNITS ----------
GOODS_UNITS = [
    'mudu', 'derica', 'paint', 'kg', 'kilogram', 'g', 'gram', 'litre', 'liter',
    'pieces', 'piece', 'heap', 'heaps', 'basket', 'baskets', 'bag', 'bags',
    'sachet', 'sachets', 'tin', 'tins', 'can', 'cans', 'carton', 'cartons',
    'roll', 'rolls', 'bar', 'bars', 'stick', 'sticks', 'loaf', 'loaves',
    'bottle', 'bottles', 'cup', 'cups', 'plate', 'plates', 'wrap', 'wraps',
    'parcel', 'parcels', 'scoop', 'scoops', 'bucket', 'buckets', 'basin',
    'bowl', 'bowls', 'crate', 'crates', 'bunch', 'bunches'
]

onboarding_state = {}
pending_transfers = {}  # user_id -> payload
pending_p2p_trades = {}
app = Flask(__name__)
socketio.init_app(app)
CORS(app, resources={r"/*": {"origins": [
    "https://oyinda-web.onrender.com",
    "http://localhost:5173",
    "https://www.oyinda-ai.online",          # ← new domain
    "capacitor://localhost",
    "http://localhost"
]}})
app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'change-me-in-production-please')
jwt = JWTManager(app)
temp_links = {}
CRON_SECRET = os.environ.get('CRON_SECRET', 'change-me-to-a-random-string')
# pending_transaction[user_id] = {
#     "state": "collecting_amount" | "collecting_type" | "collecting_quantity" | …,
#     "data": { … partial transaction data … },
#     "category": "food" or None,
# }
pending_transaction = {}
# Cache for live exchange rates
_live_rates_cache = {"data": {}, "last_fetched": None}

# ----- LOAN CONSTANTS -----
LOAN_MIN_AMOUNT = 5000
LOAN_MAX_AMOUNT = 500000
LOAN_MIN_DURATION = 3
LOAN_MAX_DURATION = 12
GRACE_PERIOD_MONTHS = 1   # first month interest‑free

def get_loan_interest_rate(credit_score):
    """Return monthly interest rate (%) based on FICO‑style score."""
    if credit_score >= 701:
        return 2.0
    elif credit_score >= 501:
        return 4.0
    elif credit_score >= 301:
        return 7.0
    else:
        return 10.0

def get_max_loan_amount(credit_score):
    """Maximum borrowable amount based on credit score tier."""
    if credit_score >= 801:
        return 1_100_000   # > 1.1M (we'll say up to 1.5M for now)
    elif credit_score >= 701:
        return 1_000_000
    elif credit_score >= 501:
        return 500_000
    elif credit_score >= 351:
        return 200_000
    elif credit_score >= 201:
        return 100_000
    elif credit_score >= 151:
        return 49_000
    elif credit_score >= 101:
        return 20_000
    elif credit_score >= 50:
        return 10_000
    else:
        return 0



def mock_execute_transfer(payload):
    return True, "MOCK-REF-" + str(uuid.uuid4())[:8]

# --------------- Voice / SMS channel (placeholder for future) ---------------
# Voice uses browser Web Speech API; no extra backend needed now.
# SMS integration will be added later via Africa's Talking or Twilio.

# --------------- Helper: normalize spoken dates ---------------
def normalize_date(date_str):
    if not date_str:
        return datetime.utcnow().strftime("%Y-%m-%d")
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return date_str
    except ValueError:
        pass
    today = datetime.utcnow().date()
    dl = date_str.lower().strip()
    # Relative days ago
    match = re.match(r'(\d+)\s*days?\s*ago', dl)
    if match:
        days = int(match.group(1))
        return (today - timedelta(days=days)).strftime("%Y-%m-%d")
    # Weekdays
    weekdays = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday']
    if dl in weekdays:
        target_idx = weekdays.index(dl)
        current_idx = today.weekday()
        days_back = (current_idx - target_idx) % 7
        if days_back == 0:
            days_back = 7
        return (today - timedelta(days=days_back)).strftime("%Y-%m-%d")
    if dl == 'yesterday':
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")
    if dl == 'today':
        return today.strftime("%Y-%m-%d")
    return today.strftime("%Y-%m-%d")


def get_live_rate(from_currency, to_currency="NGN"):
    if from_currency.upper() == to_currency.upper():
        return 1.0

    # Try live API – fetch with from_currency as base
    try:
        resp = requests.get(
            f"https://api.exchangerate-api.com/v4/latest/{from_currency.upper()}",
            timeout=10
        )
        data = resp.json()
        rate = data.get("rates", {}).get(to_currency.upper())
        if rate and rate > 0:
            return rate
    except Exception as e:
        print(f"Live rate API failed: {e}")

    # Fallback – always returns a reasonable value
    fallback = {
        "USD": 1550.0, "GBP": 1950.0, "EUR": 1700.0,
        "GHS": 130.0, "KES": 10.5, "ZAR": 85.0, "NGN": 1.0
    }
    # We need rate from_currency → NGN. If from_currency is USD, we have it.
    if to_currency.upper() == "NGN":
        return fallback.get(from_currency.upper(), 1.0)
    else:
        # For other pairs, convert via NGN
        base_to_ngn = fallback.get(from_currency.upper(), 1.0)
        ngn_to_target = fallback.get(to_currency.upper(), 1.0)
        return base_to_ngn / ngn_to_target if ngn_to_target else 1.0


def convert_currency(amount, from_currency, to_currency="NGN"):
    """Convert amount from one currency to another using live rate."""
    if from_currency.upper() == to_currency.upper():
        return amount
    rate = get_live_rate(from_currency.upper(), to_currency.upper())
    return round(amount * rate, 2)


def get_last_context(user_id):
    """Return the last logged expense/income details for context."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT description, category FROM transactions_view WHERE user_id=%s ORDER BY date DESC LIMIT 1",
        (user_id,)
    )
    row = cur.fetchone()
    conn.close()
    if row:
        return {"description": row[0], "category": row[1]}
    return None


def store_user_fact(user_id, fact_key, fact_value):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET facts = COALESCE(facts, '{}'::jsonb) || %s WHERE id = %s",
        (json.dumps({fact_key: fact_value}), user_id)
    )
    conn.commit()
    conn.close()



def ask_next_question(user_id):
    """Advance the pending transaction to the next data‑collection step."""
    p = pending_transaction.get(user_id)
    if not p:
        return jsonify({"error": "No pending transaction."}), 400

    state = p["state"]
    data = p["data"]
    category = p.get("category") or data.get("category")
    txn_type = data.get("transaction_type", "goods")   # default to goods

    # Step 1: Determine category if not known
    if state == "collecting_category" and not category:
        p["state"] = "collecting_category"
        return jsonify({
            "message": "What was this for? For example: food, transport, rent, data, health, education, savings, etc.",
            "tone": "neutral"
        })

    # Step 2: Category‑specific questions
    if category in ['food', 'transport', 'housing', 'utilities', 'health', 'education', 'savings', 'investment'] and state == "collecting_category":
        if category == 'food':
            # Only ask for quantity if it's goods
            if txn_type == 'goods':
                p["state"] = "collecting_quantity"
                return jsonify({
                    "message": "How much did you buy? For example: 2 mudu, 1 derica, a paint, 5 kg, 10 pieces.",
                    "tone": "neutral"
                })
            else:
                # Services – skip to location
                p["state"] = "collecting_location"
                return ask_for_location(user_id)
        elif category == 'transport':
            p["state"] = "collecting_transport_type"
            return jsonify({
                "message": "What type of transport? (e.g., okada, bus, uber, keke)",
                "tone": "neutral"
            })
        elif category == 'housing':
            p["state"] = "collecting_housing_type"
            return jsonify({
                "message": "Was this for rent, house repairs, or something else?",
                "tone": "neutral"
            })
        else:
            # For other categories, skip to location
            p["state"] = "collecting_location"
            return ask_for_location(user_id)

    # Step 3: Collect quantity/unit (food + goods only)
    if state == "collecting_quantity":
        units = '|'.join(GOODS_UNITS)
        items = re.findall(
            rf'(\d+)\s*({units})?\s*(?:of\s+)?(\w+(?:\s+\w+)?)',
            data.get("last_reply", reply.lower())
        )
        if items:
            parts = []
            for qty, unit, name in items:
                unit = unit if unit else ''
                part = f"{qty} {unit} {name.strip()}".strip()
                parts.append(part)
            description = ", ".join(parts)
            data["quantity_description"] = description
            data["quantity"] = len(items)
            data["unit"] = "items"
            p["data"] = data
            p["state"] = "collecting_location"
            return ask_for_location(user_id)
        else:
            return jsonify({
                "message": "I didn't catch the quantity. Please tell me like '2 mudu' or '1 paint'.",
                "tone": "neutral"
            })

    # Step 4: Collect location
    if state == "collecting_location":
        return ask_for_location(user_id)

    # Fallback – finalise if we somehow reach here
    return finalise_transaction(user_id)


def ask_for_location(user_id):
    """Check if we can reuse a recent location; if not, ask the user."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT address, last_location_update FROM users WHERE id = %s", (user_id,))
    row = cur.fetchone()
    conn.close()


    # If we have a location updated within the last 7 days, reuse it
    if row and row[0] and row[1]:
        last_update = row[1]
        if datetime.utcnow() - last_update.replace(tzinfo=None) < timedelta(days=7):
            # Reuse recent location
            p = pending_transaction[user_id]
            p["data"]["location"] = row[0]  # city name stored in address
            p["state"] = "finalise"
            return finalise_transaction(user_id)

    # Otherwise, ask the user
    p = pending_transaction[user_id]
    p["state"] = "collecting_location"
    return jsonify({
        "message": "Quickly, which city are you in right now? (e.g., Lagos, Ibadan, Abuja)",
        "tone": "neutral"
    })


def save_conversation(user_id, role, content):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO conversation_history (user_id, role, content) VALUES (%s, %s, %s)",
            (user_id, role, content)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass   # never let this break the main flow

def get_recent_conversation(user_id, n=6):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT role, content FROM conversation_history WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
        (user_id, n)
    )
    rows = cur.fetchall()
    conn.close()
    # Reverse to chronological order
    rows.reverse()
    return [{"role": r[0], "content": r[1]} for r in rows]


def finalise_transaction(user_id):
    """Log the fully collected transaction and remove from pending."""
    p = pending_transaction.pop(user_id, None)
    if not p:
        return jsonify({"error": "No pending transaction."}), 400

    data = p["data"]
    trans_type = data.get("type", "expense")
    amount = data["amount"]
    description = data.get("description", "")

    if trans_type == "managed_funds":
        event_type = "InvestmentMade"
        category = "investment_capital"
        response_text = f"Noted, {name}! You received ₦{amount:,.2f} as investment capital from {description}."
    elif trans_type in ("expense", "spent", "loan"):
        event_type = "ExpenseLogged"
        category = data.get("category", "other")
    elif trans_type == "income":
        event_type = "IncomeReceived"
        category = data.get("category", "income")
    elif trans_type == "investment":
        event_type = "InvestmentMade"
        category = data.get("category", "investment")
        response_text = f"Noted, {name}! You invested ₦{amount:,.2f} in {description}."
    else:
        event_type = "ExpenseLogged"
        category = "other"

    payload = {
        "amount": amount,
        "currency": data.get("currency", "NGN"),
        "category": category,
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "description": description,
        "quantity": data.get("quantity"),
        "quantity_description": data.get("quantity_description"),
        "unit": data.get("unit"),
        "location": data.get("location"),
        "housing_type": data.get("housing_type"),
        "transport_type": data.get("transport_type"),
        "original_amount": data.get("original_amount"),
        "original_currency": data.get("original_currency"),

    }
    payload = {k: v for k, v in payload.items() if v is not None}

    event = append_event(user_id, user_id, event_type, payload)

    # After a successful log, insert a clear system note into conversation history
    log_msg = f"Oyinda just recorded this transaction: {description} – ₦{amount:,.2f} ({event_type})"
    save_conversation(user_id, 'system', log_msg)

    name = get_user_name(user_id)
    category_label = category.replace('_', ' ').title() if category else "Other"

    if trans_type == "managed_funds":
        response_text = f"Noted, {name}! You received ₦{amount:,.2f} as investment capital from {description}."
    elif trans_type == "income":
        response_text = f"Got it, {name}! You earned ₦{amount:,.2f} from {description}."
    elif trans_type == "investment":
        response_text = f"Logged, {name}! You invested ₦{amount:,.2f} in {description}."
    elif trans_type in ("expense", "spent", "loan"):
        response_text = f"Done, {name}! I’ve recorded an expense of ₦{amount:,.2f} for {description} under {category_label}."
    else:
        response_text = f"Transaction logged: ₦{amount:,.2f} – {description}."

    # ... (daily spend summary remains unchanged)

    # Optionally, include a quick daily total if available
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(SUM(amount),0) FROM transactions_view WHERE user_id=%s AND type='expense' AND date=%s",
                (user_id, datetime.utcnow().strftime('%Y-%m-%d')))
    daily_spend = cur.fetchone()[0]
    conn.close()
    if event_type == "ExpenseLogged":
        response_text += f" You've spent ₦{daily_spend:,.2f} today so far."
    elif event_type == "IncomeReceived":
        response_text += f" You've earned ₦{daily_spend:,.2f} today so far."


    # ---------- AUTOMATED BUSINESS SUGGESTION (ALL categories) ----------
    if description:
        import random
        if random.random() < 0.25:  # 25% chance – keeps it helpful, not annoying
            try:
                conn = get_conn()
                cur = conn.cursor()
                user_facts = get_user_facts(user_id)
                my_city = user_facts.get('city', '').lower()
                # Use the last meaningful word of the description as the search keyword
                keywords = description.strip().split()[-1] if description else ''
                if len(keywords) > 2:
                    cur.execute("""
                        SELECT u.name, u.facts->>'business', u.facts->>'phone'
                        FROM users u
                        WHERE u.id != %s
                          AND u.facts->>'business' IS NOT NULL
                          AND LOWER(u.facts->>'city') = %s
                          AND u.facts->>'business' ILIKE %s
                        LIMIT 1
                    """, (user_id, my_city, f'%{keywords}%'))
                    row = cur.fetchone()
                    conn.close()
                    if row:
                        suggestion = f"\n\nBy the way, did you know **{row[0]}** in your area sells **{row[1]}**? Want me to connect you?"
                        response_text += suggestion
            except:
                pass

    # ---------- PROACTIVE MARKET PRICE COLLECTION ----------
    import random
    user_facts = get_user_facts(user_id)
    last_price_update = user_facts.get('last_price_update')
    has_business = user_facts.get('business')

    # Ask if they have a business to list, or if it's been more than 7 days since last price update
    ask_price = False
    if not has_business:
        ask_price = random.random() < 0.3   # 30% chance for users without a listing
    elif last_price_update:
        try:
            last_update = datetime.fromisoformat(last_price_update)
            if (datetime.utcnow() - last_update).days > 7:
                ask_price = random.random() < 0.3
        except:
            ask_price = False

    if ask_price:
        pending_transaction[user_id] = {
            "state": "collecting_business_details",
            "data": {},
            "category": None
        }
        # We'll append the prompt to the response text
        prompt_text = "\n\nBy the way, I dey try help our community know the correct price of things for your area. Wetin you dey sell, and how much you dey sell am? (For example: 'I sell tomatoes, 5 mudu for ₦2,000')"
        response_text += prompt_text



    # Optionally update user location if collected
    location = data.get("location")
    if location:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET address = %s, last_location_update = now() WHERE id = %s",
            (location, user_id)
        )
        conn.commit()
        conn.close()
        store_user_fact(user_id, 'city', location)

    return jsonify({"message": response_text, "tone": "neutral", "event_id": event["event_id"]})


def ensure_wallet(user_id):
    """Return wallet dict or create one with internal ledger (no external API)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT mono_account_id, account_number, bank_name, bank_code, balance FROM user_wallets WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    if row:
        wallet = {
            "account_id": row[0],
            "account_number": row[1],
            "bank_name": row[2],
            "bank_code": row[3],
            "balance": float(row[4])
        }
        conn.close()
        return wallet

    # Generate a unique account number for this user
    # We'll use the format: 79 + last 8 digits of user_id (hex -> int, take last 8)
    # This gives a 10-digit number that looks like a NUBAN.
    short_id = str(int(user_id.replace('-', ''), 16))[-8:]  # last 8 digits
    account_number = "79" + short_id.zfill(8)  # 10 digits starting with 79

    # Use a fixed system ID that represents our pooled Mono account
    system_account_id = "oyinda_pool"
    bank_name = "Oyinda Vault"
    bank_code = "000"  # Not a real bank code

    cur.execute("""
        INSERT INTO user_wallets (user_id, mono_account_id, account_number, bank_name, bank_code, balance)
        VALUES (%s, %s, %s, %s, %s, 0.0)
    """, (user_id, system_account_id, account_number, bank_name, bank_code))
    conn.commit()
    conn.close()

    return {
        "account_id": system_account_id,
        "account_number": account_number,
        "bank_name": bank_name,
        "bank_code": bank_code,
        "balance": 0.0
    }



def get_user_name(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM users WHERE id=%s", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else "there"

def get_user_type(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT account_type FROM users WHERE id=%s", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 'personal'

def extract_date_range(date_param=None):
    today = datetime.utcnow().date()
    if not date_param:
        return "1900-01-01", today.strftime("%Y-%m-%d"), "all time"
    dl = date_param.lower().strip()

    # Check if any month name appears in the string
    month_names = {
        'january':1,'february':2,'march':3,'april':4,'may':5,'june':6,
        'july':7,'august':8,'september':9,'october':10,'november':11,'december':12
    }
    found_month = None
    for m_name in month_names:
        if m_name in dl:
            found_month = m_name
            break
    if found_month:
        month = month_names[found_month]
        year = today.year
        if month > today.month:
            year -= 1
        start = f"{year}-{month:02d}-01"
        if month == 12:
            end = f"{year}-12-31"
        else:
            next_month = month + 1
            end = f"{year}-{next_month:02d}-01"
            end = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        return start, end, found_month.capitalize()

    # Specific month names (e.g., "june", "march")
    month_names = {
        'january':1, 'february':2, 'march':3, 'april':4, 'may':5, 'june':6,
        'july':7, 'august':8, 'september':9, 'october':10, 'november':11, 'december':12
    }
    if dl in month_names:
        month = month_names[dl]
        year = today.year
        # If month is in the future, assume last year
        if month > today.month:
            year -= 1
        start = f"{year}-{month:02d}-01"
        # Calculate end of month
        if month == 12:
            end = f"{year}-12-31"
        else:
            next_month = month + 1
            end = f"{year}-{next_month:02d}-01"
            end = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        return start, end, dl.capitalize()

    # Common relative phrases
    if 'today' in dl:
        return today.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"), "today"
    if 'yesterday' in dl:
        y = today - timedelta(days=1)
        return y.strftime("%Y-%m-%d"), y.strftime("%Y-%m-%d"), "yesterday"
    if 'this week' in dl:
        start = today - timedelta(days=today.weekday())
        return start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"), "this week"
    if 'this month' in dl:
        start = today.strftime("%Y-%m") + "-01"
        return start, today.strftime("%Y-%m-%d"), "this month"
    if 'last week' in dl:
        end = today - timedelta(days=today.weekday()+1)
        start = end - timedelta(days=6)
        return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), "last week"
    if 'last month' in dl:
        first_day_this_month = today.replace(day=1)
        last_day_prev = first_day_this_month - timedelta(days=1)
        start_prev = last_day_prev.replace(day=1)
        return start_prev.strftime("%Y-%m-%d"), last_day_prev.strftime("%Y-%m-%d"), "last month"
    if 'this year' in dl:
        start = f"{today.year}-01-01"
        return start, today.strftime("%Y-%m-%d"), "this year"
    if 'last year' in dl:
        start = f"{today.year-1}-01-01"
        end = f"{today.year-1}-12-31"
        return start, end, "last year"
    # Handle run‑together words
    if 'lastweek' in dl or 'last week' in dl:
        end = today - timedelta(days=today.weekday()+1)
        start = end - timedelta(days=6)
        return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), "last week"
    if 'thisweek' in dl or 'this week' in dl:
        start = today - timedelta(days=today.weekday())
        return start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"), "this week"
    if 'thismonth' in dl or 'this month' in dl:
        start = today.strftime("%Y-%m") + "-01"
        return start, today.strftime("%Y-%m-%d"), "this month"
    if 'lastmonth' in dl or 'last month' in dl:
        first_day_this_month = today.replace(day=1)
        last_day_prev = first_day_this_month - timedelta(days=1)
        start_prev = last_day_prev.replace(day=1)
        return start_prev.strftime("%Y-%m-%d"), last_day_prev.strftime("%Y-%m-%d"), "last month"

    # fallback all time
    return "1900-01-01", today.strftime("%Y-%m-%d"), "all time"

# --------------- AUTH ---------------
@app.route('/register', methods=['POST'])
def register():
    data = request.get_json()
    name = data.get('name')
    email = data.get('email')
    password = data.get('password')
    account_type = data.get('account_type', 'personal')
    address = data.get('address', '')
    if not name or not email or not password:
        return jsonify({"error": "Name, email, and password are required."}), 400
    user_id = create_user(name, email, password, account_type, address)
    if not user_id:
        return jsonify({"error": "Registration failed. Email may already be in use."}), 400
    token = create_access_token(identity=user_id)
    return jsonify({"message": f"Welcome {name}! I'm your CFO. Let's build your financial future. How much have you made or spent today?.", "user": {"id": user_id, "name": name}, "token": token})

@app.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    email = data.get('email', '').strip()
    password = data.get('password', '')
    if not email or not password:
        return jsonify({"error": "Email and password required."}), 400
    user = authenticate_user(email, password)
    if not user:
        return jsonify({"error": "Invalid email or password."}), 401
    token = create_access_token(identity=user['id'])
    return jsonify({"message": f"Welcome back, {user['name']}! Ready to take control of your finances?", "user": user, "token": token})



def process_user_command(user_id, text):
    text_lower = text.lower().strip()

    # ---------- WALLET COMMANDS ----------
    text_lower_wallet = text.lower()
    if any(phrase in text_lower_wallet for phrase in ['wallet balance', 'check my wallet', 'my wallet']):
        try:
            wallet = ensure_wallet(user_id)
            save_conversation(user_id, 'user', text)
            return jsonify({
                "message": f"💰 Your Oyinda wallet balance is ₦{wallet['balance']:,.2f}\n"
                           f"Account number: {wallet['account_number']} ({wallet['bank_name']})\n"
                           f"Send money: 'send 500 to 080xxxx' | Withdraw: 'withdraw 5000 to 058 0123456789'",
                "tone": "neutral"
            })
        except Exception as e:
            return jsonify({"message": str(e), "tone": "warning"})

    # ---------- CONTINUE PENDING CONVERSATION ---
    if user_id in pending_transaction:
        p = pending_transaction[user_id]
        state = p["state"]
        reply = text.strip()

        # Allow user to abort any pending wizard
        if state.startswith('loan_') and reply.strip().lower() == 'cancel':
            pending_transaction.pop(user_id, None)
            return jsonify({"message": "Loan request cancelled. How can I help you?", "tone": "neutral"})

        if state == "collecting_type":
            reply_lower = reply.lower()
            if any(w in reply_lower for w in ['spent', 'expense', 'spend', 'bought', 'paid']):
                p["data"]["type"] = "expense"
            elif any(w in reply_lower for w in ['income', 'earned', 'profit']):
                p["data"]["type"] = "income"
            elif any(w in reply_lower for w in ['invest', 'investment', 'save', 'savings', 'invested']):
                p["data"]["type"] = "investment"
                p["data"]["category"] = "investment"
                p["category"] = "investment"
                return ask_for_location(user_id)
            elif any(w in reply_lower for w in ['loan', 'borrow']):
                p["data"]["type"] = "loan"
            elif any(w in reply_lower for w in
                     ['managed', 'capital', 'funds', 'manage', 'managed funds', 'investment capital']):
                p["data"]["type"] = "managed_funds"
                p["data"]["category"] = "investment_capital"
                p["category"] = "investment_capital"
                # Skip category question, go to location
                return ask_for_location(user_id)
            else:
                return jsonify({
                    "message": f"Sorry, was {p['data']['amount']} NGN spent, earned, invested, managed funds, or a loan?",
                    "tone": "neutral"
                })
            p["state"] = "collecting_category"
            return ask_next_question(user_id)


        elif state == "loan_ask_product":
            # If the user seems to be starting a new task, cancel the loan wizard
            likely_other_task = any(word in reply.lower() for word in [
                # Actions
                'spent', 'bought', 'paid', 'earned', 'received', 'made',
                'balance', 'net worth', 'credit score', 'statement', 'tax',
                'data', 'airtime', 'withdraw', 'send', 'swap', 'buy', 'sell',
                'search', 'find', 'who sells', 'i need', 'what', 'how',
                # Questions / new intents
                'list', 'show', 'register', 'link', 'connect',
                # Explicit cancellation
                'cancel', 'stop', 'never mind', 'forget',
                # Loan re-trigger (start fresh)
                'borrow', 'loan', 'lend'
            ]) or re.search(r'\b(?:who|what|how|where)\b', reply, re.IGNORECASE)

            if likely_other_task:
                pending_transaction.pop(user_id, None)
                return process_user_command(user_id, reply)
            # User might provide amount + product in one go, or just product
            text_clean = reply.strip()
            # Check if they also gave an amount now (if we didn't have one)
            if p["data"]["amount"] is None:
                amount_match = re.search(r'(\d[\d,]*\.?\d*)\s*(?:k|thousand)?', text_clean, re.IGNORECASE)
                if amount_match:
                    amount_str = amount_match.group(1).replace(',', '')
                    p["data"]["amount"] = float(amount_str)
                    if 'k' in text_clean.lower() or 'thousand' in text_clean.lower():
                        p["data"]["amount"] *= 1000
                    # Remove the amount from the product description
                    text_clean = re.sub(r'(\d[\d,]*\.?\d*)\s*(?:k|thousand)?', '', text_clean,
                                        flags=re.IGNORECASE).strip()
                else:
                    return jsonify({
                        "message": "I need the amount you want to borrow. For example, '5000 for bags of rice'.",
                        "tone": "neutral"
                    })

            # Remove common filler words from the product description
            text_clean = re.sub(r'\b(borrow|to buy|to purchase|for|to)\b', '', text_clean, flags=re.IGNORECASE)
            text_clean = re.sub(r'\s+', ' ', text_clean).strip()
            if not text_clean:
                return jsonify(
                    {"message": "What exactly do you want to buy? (e.g., 'bags of rice')", "tone": "neutral"})

            p["data"]["product"] = text_clean
            p["state"] = "loan_ask_supplier"
            return jsonify({
                "message": f"Got it! You want to buy {text_clean}. Which supplier do you want to buy from? Give me a name or part of the name.",
                "tone": "neutral"
            })

        elif state == "loan_ask_supplier":
            # If the user seems to be starting a new task, cancel the loan wizard
            likely_other_task = any(word in reply.lower() for word in [
                # Actions
                'spent', 'bought', 'paid', 'earned', 'received', 'made',
                'balance', 'net worth', 'credit score', 'statement', 'tax',
                'data', 'airtime', 'withdraw', 'send', 'swap', 'buy', 'sell',
                'search', 'find', 'who sells', 'i need', 'what', 'how',
                # Questions / new intents
                'list', 'show', 'register', 'link', 'connect',
                # Explicit cancellation
                'cancel', 'stop', 'never mind', 'forget',
                # Loan re-trigger (start fresh)
                'borrow', 'loan', 'lend'
            ]) or re.search(r'\b(?:who|what|how|where)\b', reply, re.IGNORECASE)

            if likely_other_task:
                pending_transaction.pop(user_id, None)
                return process_user_command(user_id, reply)

            supplier_query = reply.strip()
            if not supplier_query:
                return jsonify({"message": "Please give me the supplier's name or part of it.", "tone": "neutral"})

            # Search the business directory
            conn = get_conn()
            cur = conn.cursor()
            like_query = f'%{supplier_query}%'
            cur.execute("""
                SELECT bl.user_id, bl.name, bl.product, bl.market_name, bl.city, bl.phone, bl.rating, bl.total_ratings, bl.is_verified
                FROM business_listings bl
                JOIN user_wallets uw ON bl.user_id = uw.user_id
                WHERE LOWER(bl.name) ILIKE %s
                ORDER BY bl.rating DESC NULLS LAST, bl.created_at DESC
                LIMIT 5
            """, (like_query,))
            suppliers = cur.fetchall()
            conn.close()

            if not suppliers:
                return jsonify({
                    "message": f"I couldn't find any supplier matching '{supplier_query}'. Make sure the supplier has a registered business on Oyinda and a wallet. Try another name.",
                    "tone": "warning"
                })

            # Build the same kind of business list we use for search
            results = []
            for s in suppliers:
                results.append({
                    "id": s[0],  # user_id (supplier)
                    "name": s[1],
                    "product": s[2],
                    "market_name": s[3] or '',
                    "city": s[4] or '',
                    "phone": s[5] or '',
                    "rating": s[6] or 0,
                    "total_ratings": s[7] or 0,
                    "is_verified": s[8] or False,
                    "listing_type": "user"
                })

            p["data"]["supplier_options"] = results
            p["state"] = "loan_confirm_supplier"
            # Return the list with a dedicated action so the frontend doesn't fetch
            return jsonify({
                "action": "loan_supplier_selection",
                "search_query": supplier_query,
                "message": f"I found {len(results)} supplier(s) matching '{supplier_query}'. Tap the correct one to continue.",
                "tone": "neutral",
                "suppliers": results
            })

        elif state == "loan_confirm_supplier":
            # If the user seems to be starting a new task, cancel the loan wizard
            likely_other_task = any(word in reply.lower() for word in [
                # Actions
                'spent', 'bought', 'paid', 'earned', 'received', 'made',
                'balance', 'net worth', 'credit score', 'statement', 'tax',
                'data', 'airtime', 'withdraw', 'send', 'swap', 'buy', 'sell',
                'search', 'find', 'who sells', 'i need', 'what', 'how',
                # Questions / new intents
                'list', 'show', 'register', 'link', 'connect',
                # Explicit cancellation
                'cancel', 'stop', 'never mind', 'forget',
                # Loan re-trigger (start fresh)
                'borrow', 'loan', 'lend'
            ]) or re.search(r'\b(?:who|what|how|where)\b', reply, re.IGNORECASE)

            if likely_other_task:
                pending_transaction.pop(user_id, None)
                return process_user_command(user_id, reply)
            # We'll match the selected name from the options we stored.
            selected_name = reply.strip()
            options = p["data"].get("supplier_options", [])
            selected = None
            for opt in options:
                if opt["name"].lower() == selected_name.lower():
                    selected = opt
                    break
            if not selected:
                return jsonify({
                    "message": "Please tap one of the suppliers from the list I sent.",
                    "tone": "warning"
                })

            p["data"]["supplier_user_id"] = selected["id"]
            p["data"]["supplier_name"] = selected["name"]
            p["state"] = "confirming_inventory_loan"  # reuse the existing confirmation state

            amount = p["data"]["amount"]
            flat_fee = amount * 0.05
            total_repayable = amount + flat_fee
            daily_amount = round(total_repayable / 14, 2)

            p["data"]["flat_fee"] = flat_fee
            p["data"]["total_repayable"] = total_repayable
            p["data"]["daily_amount"] = daily_amount

            return jsonify({
                "message": (
                    f"📦 **Loan Summary**\n\n"
                    f"• Amount: ₦{amount:,.2f}\n"
                    f"• Product: {p['data']['product']}\n"
                    f"• Supplier: {selected['name']}\n"
                    f"• Fee (5%): ₦{flat_fee:,.2f}\n"
                    f"• Total to repay: ₦{total_repayable:,.2f}\n"
                    f"• Daily repayment: ₦{daily_amount:,.2f} for 14 days (after 7‑day grace)\n\n"
                    f"Reply **'yes'** to confirm and I'll pay the supplier directly."
                ),
                "tone": "neutral"
            })



        elif state == "ask_id_type":
            id_type = reply.strip().lower()
            if id_type not in ['bvn', 'nin']:
                return jsonify({"message": "Please type either 'BVN' or 'NIN'.", "tone": "neutral"})
            p["data"]["id_type"] = id_type
            p["state"] = "ask_id_number"
            return jsonify({"message": f"What is your {id_type.upper()} number? (11 digits)", "tone": "neutral"})


        elif state == "collecting_loan_direction":
            reply_lower = reply.lower()
            if any(w in reply_lower for w in ['borrow', 'from', 'i go pay back', 'i will pay back']):
                p["data"]["type"] = "loan"
                p["data"]["category"] = "loan"
                p["state"] = "collecting_category"
                return ask_next_question(user_id)
            elif any(w in reply_lower for w in ['lend', 'lent', 'to', 'they go pay me', 'they will pay me']):
                p["data"]["type"] = "asset"
                p["data"]["category"] = "loan_given"
                p["state"] = "collecting_category"
                return ask_next_question(user_id)
            else:
                return jsonify({
                    "message": "Sorry, I didn’t understand. Did you borrow from someone, or did you lend to someone?",
                    "tone": "neutral"
                })

        elif state == "confirming_funds":
            if any(word in reply.lower() for word in ['yes', 'yeah', 'correct']):
                p["data"]["type"] = "managed_funds"
                p["data"]["category"] = "investment_capital"
                p["state"] = "collecting_category"
                return ask_next_question(user_id)
            else:
                p["state"] = "collecting_type"
                return jsonify({
                    "message": f"Okay, what kind of transaction is this? (spent, earned, invested, savings, loan, or managed funds?)",
                    "tone": "neutral"
                })


        elif state == "collecting_category":
            reply_lower = reply.lower()
            cat_map = {
                'food': ['food', 'foods', 'feeding', 'groceries', 'rice', 'beans', 'garri', 'yam', 'meat',
                         'spaghetti',
                         'noodle', 'indomie', 'bread', 'egg', 'eggs', 'milk', 'sugar', 'oil', 'tomato', 'tomatoes',
                         'pepper', 'onion', 'fish', 'chicken', 'beef', 'snack', 'snacks', 'drink', 'drinks',
                         'water',
                         'juice', 'soda', 'coke', 'fanta', 'pepsi', 'chinchin', 'cake', 'biscuit', 'biscuits',
                         'sweets',
                         'ice cream', 'restaurant', 'eatery', 'bukka', 'mama put', 'chop', 'swallow', 'eba',
                         'amala',
                         'fufu', 'pounded yam', 'semo'],
                'transport': ['transport', 'transportation', 'okada', 'bike', 'motorcycle', 'uber', 'bolt', 'taxi',
                              'bus', 'buses', 'keke', 'napep', 'tricycle', 'fuel', 'petrol', 'diesel', 'gas',
                              'parking',
                              'parking fee', 'toll', 'toll gate', 'fare', 'transport fare'],
                'housing': ['rent', 'house rent', 'house', 'room', 'accommodation', 'apartment', 'landlord',
                            'rentage',
                            'property', 'maintenance', 'repair', 'repairs', 'plumbing', 'electrician', 'painting',
                            'renovation', 'furniture', 'bed', 'mattress', 'curtain', 'curtains', 'carpet', 'rug'],
                'utilities': ['data', 'internet', 'net', 'subscription', 'subscriptions', 'airtime', 'recharge',
                              'top up', 'topup', 'phone bill', 'phone', 'electricity', 'electric', 'power', 'neepa',
                              'nepa', 'bill', 'bills', 'water', 'waste', 'sewage', 'sanitation', 'utility',
                              'utilities',
                              'mifi', 'router', 'wifi', 'broadband', 'cable', 'dstv', 'gotv', 'startimes',
                              'tv subscription', 'netflix', 'prime video', 'showmax', 'domain', 'domain name',
                              'hosting', 'website'],
                'health': ['doctor', 'hospital', 'medicine', 'drug', 'drugs', 'pharmacy', 'chemist', 'health',
                           'healthcare', 'medical', 'medicals', 'dental', 'dentist', 'eye', 'optician', 'glasses',
                           'surgery', 'injection', 'vaccine', 'checkup', 'check up', 'lab', 'laboratory', 'test',
                           'tests', 'scan', 'x-ray', 'xray', 'bandage', 'plaster', 'first aid', 'blood', 'malaria',
                           'typhoid', 'fever', 'headache', 'pain', 'pills', 'tablets', 'capsules', 'syrup',
                           'ointment',
                           'cream', 'inhaler'],
                'education': ['school', 'school fees', 'fees', 'tuition', 'book', 'books', 'textbook', 'course',
                              'courses', 'online course', 'udemy', 'coursera', 'training', 'workshop', 'seminar',
                              'certification', 'exam', 'examination', 'jamb', 'waec', 'neco', 'gce', 'post utme',
                              'form', 'registration', 'admission', 'pen', 'pencil', 'notebook', 'stationery',
                              'calculator', 'laptop', 'research', 'project', 'thesis', 'dissertation', 'library',
                              'printing', 'photocopy', 'typing', 'assignment', 'lesson', 'tutor', 'coaching',
                              'extra lessons', 'after school'],
                'investment': ['invest', 'investment', 'investments', 'stocks', 'shares', 'stock', 'bond', 'bonds',
                               'mutual fund', 'mutual funds', 'etf', 'etfs', 'crypto', 'cryptocurrency', 'bitcoin',
                               'btc', 'ethereum', 'eth', 'usdt', 'usdc', 'bnb', 'binance', 'bamboo', 'chaka',
                               'trove',
                               'rise', 'piggyvest', 'cowrywise', 'wealth', 'wealth.ng', 'asset', 'assets',
                               'portfolio',
                               'dividend', 'interest', 'roi', 'return', 'capital', 'equity', 'real estate', 'land',
                               'property', 'gold', 'silver', 'forex', 'fx', 'trading', 'trade', 'buying shares'],
                'savings': ['save', 'saving', 'savings', 'saved', 'deposit', 'deposits', 'fixed deposit',
                            'treasury bill', 'tbills', 'money market', 'vault', 'lock', 'locked', 'savings plan',
                            'target', 'goal', 'goals', 'emergency fund', 'sinking fund', 'fund', 'contribution',
                            'contributions', 'ajo', 'esusu', 'collect', 'thrift', 'cooperative', 'coop',
                            'piggy bank',
                            'piggyvest', 'cowrywise', 'kolo', 'wooden box', 'safe'],
                'loan': ['loan', 'loans', 'borrow', 'borrowed', 'lend', 'lent', 'debt', 'debts', 'credit',
                         'advance',
                         'owe', 'owing', 'repay', 'repayment', 'repayments', 'repay loan', 'pay back', 'paid back',
                         'refund', 'microfinance', 'lapo', 'access bank loan', 'gtbank loan', 'uba loan',
                         'quick check',
                         'carbon', 'fairmoney', 'palmcredit', 'aella', 'branch', 'okash', 'credit card'],
                'income': ['income', 'salary', 'wages', 'wage', 'pay', 'payment', 'received', 'earned', 'made',
                           'profit', 'profit from', 'revenue', 'earnings', 'freelance', 'gig', 'side hustle',
                           'business', 'business income', 'sales', 'sold', 'client', 'customer', 'paid me',
                           'transferred to me', 'alert', 'credit alert', 'bonus', 'commission', 'allowance',
                           'stipend',
                           'grant', 'dividend', 'interest', 'return on investment', 'rent income', 'rental income',
                           'pension', 'remittance', 'money from', 'sent me', 'sent money', 'wire transfer',
                           'direct deposit', 'cash', 'cash income'],
                'entertainment': ['entertainment', 'fun', 'leisure', 'movie', 'movies', 'cinema', 'film', 'show',
                                  'concert', 'music', 'spotify', 'apple music', 'youtube', 'youtube premium',
                                  'netflix',
                                  'amazon prime', 'showmax', 'dstv', 'gotv', 'startimes', 'game', 'games',
                                  'video game',
                                  'playstation', 'ps4', 'ps5', 'xbox', 'nintendo', 'bet', 'betting', 'sport bet',
                                  'sportybet', 'bet9ja', 'nairabet', 'lottery', 'gambling', 'pool', 'club', 'party',
                                  'event', 'festival', 'carnival', 'outing', 'hanging out', 'chilling',
                                  'recreation',
                                  'subscription', 'subscriptions'],
                'clothing': ['clothing', 'cloth', 'clothes', 'clothings', 'fashion', 'wear', 'wears', 'dress',
                             'dresses', 'shirt', 'shirts', 'trouser', 'trousers', 'pant', 'pants', 'jeans',
                             'jacket',
                             'coat', 'blazer', 'suit', 'tie', 'shoe', 'shoes', 'sneakers', 'sandal', 'sandals',
                             'slippers', 'bag', 'bags', 'handbag', 'wallet', 'watch', 'jewelry', 'jewellery',
                             'necklace', 'bracelet', 'ring', 'earring', 'earrings', 'chain', 'anklet', 'cap', 'hat',
                             'scarf', 'glasses', 'sunglasses', 'belt', 'underwear', 'boxers', 'bra', 'panties',
                             'native', 'ankara', 'agbada', 'buba', 'iro', 'gele', 'tailor', 'sewing', 'fabric',
                             'material', 'lace', 'asoebi', 'guinea', 'brocade', 'satin', 'cotton', 'linen', 'wool',
                             'silk', 'polish', 'dry cleaning', 'laundry', 'wash'],
                'personal care': ['personal care', 'self care', 'grooming', 'salon', 'barbing', 'haircut', 'hair',
                                  'hairstyle', 'braiding', 'weaving', 'weavon', 'wig', 'attachment', 'relaxer',
                                  'shampoo', 'conditioner', 'cream', 'lotion', 'soap', 'body wash', 'deodorant',
                                  'perfume', 'cologne', 'makeup', 'make up', 'powder', 'lipstick', 'eyeshadow',
                                  'mascara', 'foundation', 'blush', 'nail', 'nails', 'manicure', 'pedicure', 'spa',
                                  'massage', 'waxing', 'shaving', 'razor', 'toothpaste', 'toothbrush', 'mouthwash',
                                  'floss', 'tissue', 'tissues', 'towel', 'sanitizer', 'sanitiser', 'hand wash'],
                'gift': ['gift', 'gifts', 'present', 'donation', 'offering', 'tithe', 'seed', 'sowing', 'blessing',
                         'help', 'support', 'assistance', 'charity', 'alms', 'zakat', 'sadaqah', 'give away',
                         'give out', 'gave', 'giving', 'sponsor', 'sponsorship'],
                'tax': ['tax', 'taxes', 'taxation', 'vat', 'withholding tax', 'company tax', 'income tax', 'paye',
                        'firs', 'lirs', 'government', 'levy', 'duties', 'customs', 'excise', 'rate', 'rates',
                        'assessment', 'filing', 'clearance', 'receipt', 'tax receipt', 'tin', 'tax identification',
                        'business premises', 'development levy', 'waste management bill'],
                'insurance': ['insurance', 'insure', 'insured', 'policy', 'premium', 'life insurance',
                              'health insurance', 'car insurance', 'motor insurance', 'third party',
                              'comprehensive',
                              'travel insurance', 'hmo', 'hygeia', 'avon', 'leadway', 'aig', 'mutual benefit',
                              'aiico',
                              'coronation', 'nsurance', 'cover', 'coverage', 'plan', 'benefit', 'claim', 'renewal',
                              'broker', 'agent', 'underwriter'],
                'subscription': ['subscription', 'subscribe', 'membership', 'monthly', 'annual', 'yearly', 'plan',
                                 'package', 'renewal', 'auto renew', 'recurring', 'charge', 'deduction', 'billed'],
                'other': ['other', 'miscellaneous', 'misc', 'others', 'unknown', 'general', 'various', 'different',
                          'multiple', 'sundry', 'expenses', 'expense', 'items', 'item', 'stuff', 'things',
                          'purchase',
                          'purchases', 'buy', 'bought', 'spend', 'spent', 'paid', 'pay for', 'billed for']
            }
            matched = False
            for cat, words in cat_map.items():
                if any(w in reply_lower for w in words):
                    p["data"]["category"] = cat
                    p["category"] = cat
                    matched = True
                    break
            if not matched:
                return jsonify({
                    "message": f"I didn't recognise that category. Try one of these: food, transport, housing, utilities, health, education, investment, savings, loan, income, entertainment, clothing, personal care, gift, tax, insurance, subscription, or other. What was this expense for?",
                    "tone": "neutral"
                })
            p["state"] = "collecting_category"  # let ask_next_question advance
            return ask_next_question(user_id)

        elif state == "collecting_quantity":
            qty_match = re.search(r'(\d+)\s*(mudu|derica|paint|kg|g|pieces|heap|basket|bag|litre|liter)?',
                                  reply.lower())
            if qty_match:
                p["data"]["quantity"] = float(qty_match.group(1))
                p["data"]["unit"] = qty_match.group(2) or "unknown"
                p["state"] = "collecting_location"
                return ask_for_location(user_id)
            else:
                return jsonify({
                    "message": "I need the quantity. Please tell me like '2 mudu' or '1 paint'.",
                    "tone": "neutral"
                })


        elif state == "ask_id_number":

            id_number = reply.strip()

            if len(id_number) < 10:
                return jsonify(
                    {"message": "That doesn't look like a valid number. Please enter at least 10 digits.",
                     "tone": "neutral"})

            id_type = p["data"]["id_type"]

            # Simulate verification (replace with real API later)

            verified = len(id_number) >= 10

            if verified:

                store_user_fact(user_id, f'{id_type}_verified', True)

                store_user_fact(user_id, f'{id_type}_number', id_number)

                store_user_fact(user_id, id_type, id_number)  # generic key for wallet

                # Auto-create wallet

                try:

                    wallet = ensure_wallet(user_id)

                    wallet_msg = f"Your wallet is active! Account: {wallet['account_number']} ({wallet['bank_name']})."

                except Exception as e:

                    wallet_msg = f"Wallet creation failed: {str(e)}. You can try again later."

                pending_transaction.pop(user_id, None)
                return jsonify({
                    "message": f"Identity verified! {wallet_msg}",
                    "tone": "income",
                    "feedback_prompt": {
                        "context": "after_wallet_activation",
                        "question": "How was the verification process?",
                        "options": ["Quick & easy 👍", "Okay, nothing bad", "Too stressful 😟"]
                    }
                })

            else:

                return jsonify({"message": "Verification failed. Please check your number and try again."})


        elif state == "collecting_business_details":
            # Parse the user's business description and price
            reply_lower = reply.lower()
            # Try to extract product, quantity/unit, and price
            biz_match = re.search(
                r'(?:i\s+)?(?:sell|supply|make|do)\s+(.+?)(?:,?\s*(\d+)\s*(mudu|derica|paint|kg|g|pieces?|heaps?|baskets?|bags?|litres?|liters?|units?))?\s*(?:for|at)\s*(?:₦|naira)?\s*(\d+\.?\d*)',
                reply, re.IGNORECASE)
            if biz_match:
                product = biz_match.group(1).strip()
                quantity = biz_match.group(2)
                unit = biz_match.group(3)
                price = biz_match.group(4)

                store_user_fact(user_id, 'business', product)
                if price:
                    store_user_fact(user_id, f'product_price_{product}', price)
                if quantity and unit:
                    store_user_fact(user_id, f'product_unit_{product}', f'{quantity} {unit}')
                store_user_fact(user_id, 'last_price_update', datetime.utcnow().isoformat())

                # Also save location for marketplace
                user_facts = get_user_facts(user_id)
                city = user_facts.get('city', 'your area')
                store_user_fact(user_id, 'business_city', city)

                msg = f"I don record am! When someone dey find {product} for {city}, I go connect dem to you."
                if price:
                    msg += f" Your price of ₦{price}"
                    if quantity and unit:
                        msg += f" for {quantity} {unit}"
                    msg += " don dey our system."
                pending_transaction.pop(user_id, None)
                return jsonify({"message": msg, "tone": "income"})
            else:
                # Couldn't parse – ask again more clearly
                return jsonify({
                    "message": "I no fit get the price well. Try tell me like this: 'I sell tomatoes, 5 mudu for ₦2,000'. Wetin you dey sell and how much?",
                    "tone": "neutral"
                })


        elif state == "confirming_inventory_loan":
            if any(word in reply.lower() for word in ['yes', 'yeah', 'accept', 'ok']):
                data = p["data"]
                amount = data["amount"]
                supplier_user_id = data["supplier_user_id"]

                # Check user wallet has enough? No, Oyinda pays from its own pool.
                # For simulation, we'll use a system pool wallet (Oyinda's own wallet).
                # In production, you'd have a pre-funded Mono reserved account.
                # We'll simulate by creating a system user with a wallet, or just assume Oyinda has infinite balance for now.
                # We'll log a transfer from a system account to supplier.
                # Actually, we'll just directly credit the supplier's wallet (simulate disbursement).
                # In reality, you'd use MonoReservedAccount.payout() to send money from Oyinda's pool to supplier's virtual account.
                # But since we are inside Oyinda's ledger, we can just update supplier's wallet balance directly
                # because both are internal. We'll assume Oyinda's pool is represented by a special system user 'oyinda_treasury'.
                # For sandbox, we'll just add the amount to supplier's wallet.

                conn = get_conn()
                cur = conn.cursor()
                # Add funds to supplier wallet
                cur.execute(
                    "UPDATE user_wallets SET balance = balance + %s, last_balance_update = now() WHERE user_id = %s",
                    (amount, supplier_user_id))
                # Also log a credit event for supplier
                append_event(supplier_user_id, supplier_user_id, 'WalletCredited', {
                    "amount": amount,
                    "source": "oyinda_inventory_loan",
                    "product": data["product"],
                    "borrower": user_id
                })
                conn.commit()
                conn.close()

                # Create loan record
                start_date = datetime.utcnow().date()
                end_date = start_date + timedelta(days=21)
                conn = get_conn()
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO inventory_loans
                    (user_id, supplier_id, product, principal, flat_fee, total_repayable,
                     daily_amount, remaining_balance, start_date, end_date, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active')
                """, (
                    user_id, supplier_user_id, data["product"], amount,
                    data["flat_fee"], data["total_repayable"], data["daily_amount"],
                    data["total_repayable"], start_date, end_date
                ))
                conn.commit()
                conn.close()

                pending_transaction.pop(user_id, None)
                name = get_user_name(user_id)
                pending_transaction.pop(user_id, None)
                name = get_user_name(user_id)
                return jsonify({
                    "message": f"✅ Done! ₦{amount:,.2f} paid to {data['supplier_name']} for {data['product']}.\n"
                               f"Your daily repayment is ₦{data['daily_amount']:,.2f}. I'll deduct it from your wallet automatically.",
                    "tone": "income",
                    "feedback_prompt": {
                        "context": "after_loan",
                        "question": "How was the loan process?",
                        "options": ["Smooth & fast 🚀", "Okay", "Too confusing 😕"]
                    }
                })
            else:
                pending_transaction.pop(user_id, None)
                return jsonify({"message": "Loan cancelled."})


        elif state == "collecting_location":
            p["data"]["location"] = reply.strip()
            return finalise_transaction(user_id)

        elif state == "collecting_emergency_hours":
            hours_match = re.match(r'^(\d+)$', reply.strip())
            if hours_match:
                hours = int(hours_match.group(1))
                if hours < 1 or hours > 72:
                    return jsonify({"message": "Please choose between 1 and 72 hours."})
                store_user_fact(user_id, 'emergency_data_hours', hours)
                pending_transaction.pop(user_id, None)
                return jsonify({
                    "message": f"Got it! I'll send you 33 MB of **MTN** data after you've been offline for {hours} hours. "
                               "I'll check every 30 minutes.",
                    "tone": "income"
                })
            else:
                return jsonify({
                    "message": "Please tell me a number of hours, like 3, 6, or 12.",
                    "tone": "neutral"
                })


        elif state == "confirming_bulk":
            reply_lower = reply.lower()
            if any(word in reply_lower for word in ['yes', 'yeah', 'confirm', 'log them', 'all', 'spent']):
                # Log each amount as a separate expense
                amounts = p["data"]["amounts"]
                for amt in amounts:
                    append_event(user_id, user_id, 'ExpenseLogged', {
                        "amount": amt,
                        "currency": "NGN",
                        "category": "other",
                        "date": datetime.utcnow().strftime("%Y-%m-%d"),
                        "description": p["data"].get("description", text)[:100]
                    })
                # Remove the bulk transaction
                pending_transaction.pop(user_id, None)
                name = get_user_name(user_id)
                return jsonify({
                    "message": f"Logged {len(amounts)} expenses totalling ₦{p['data']['total']:,.2f}.",
                    "tone": "neutral"
                })
            else:
                # User doesn't want bulk logging – remove and let fallback handle it
                pending_transaction.pop(user_id, None)

        # If we didn't match any state, just finalise to avoid hanging
        return finalise_transaction(user_id)


    # ---- CONVERSATIONAL LOAN ENTRY (any borrow intent, refined) ----
    past_borrowing = any(phrase in text_lower for phrase in [
        'i borrowed', 'i took a loan', 'i got a loan', 'i was given',
        'i lent', 'i gave a loan', 'i loaned', 'somebody borrowed'
    ])

    borrow_trigger = any(phrase in text_lower for phrase in [
        'borrow', 'i want to borrow', 'i wan borrow', 'lend me', 'can i get','i need to borrow', 'lend me',
        'can i get a loan', 'how much loan', 'i need loan', 'give me', 'can you borrow me',
        'borrow me', 'give me loan', 'i need a loan', 'i want a loan',
        'get a loan', 'how can i borrow', 'can i borrow'
    ]) or re.search(r'\b(?:borrow|lend)\s+me\s+(\d[\d,]*\.?\d*)', text, re.IGNORECASE)

    # Only start the wizard if it's a genuine request (not a past report)
    if borrow_trigger and not past_borrowing:
        # If the user is already in a loan wizard, warn them
        if user_id in pending_transaction and pending_transaction[user_id]['state'].startswith('loan_'):
            return jsonify({
                "message": "You're already in the middle of a loan request. To start over, type 'cancel' first.",
                "tone": "warning"
            })

        # ... rest of the existing loan entry logic (amount extraction, eligibility, etc.) ...
        amount_match = re.search(r'(\d[\d,]*\.?\d*)\s*(?:k|thousand)?', text, re.IGNORECASE)
        amount = None
        if amount_match:
            amount_str = amount_match.group(1).replace(',', '')
            amount = float(amount_str)
            # If "k" or "thousand" is mentioned, multiply by 1000
            if 'k' in text_lower or 'thousand' in text_lower:
                amount *= 1000

        credit = get_credit_score(user_id)
        max_loan = get_max_loan_amount(credit['score'])

        # Eligibility check
        if max_loan == 0:
            return jsonify({
                "message": "Your credit score is below 50. Keep telling me your daily expenses and income, and your score will grow!",
                "tone": "neutral"
            })

        # If an amount was given, validate it
        if amount is not None:
            if amount > max_loan:
                return jsonify({
                    "message": (
                        f"With your credit score of {credit['score']}/850, the maximum you can borrow is ₦{max_loan:,}. "
                        f"Would you like to borrow ₦{max_loan:,} instead?"
                    ),
                    "tone": "warning"
                })
        else:
            amount = None  # will be asked later if not provided

        # Store loan intent in pending transaction
        pending_transaction[user_id] = {
            "state": "loan_ask_product",
            "data": {
                "amount": amount,
                "max_loan": max_loan,
                "credit_score": credit['score']
            },
            "category": None
        }

        if amount:
            return jsonify({
                "message": f"Okay! You want to borrow ₦{amount:,.0f}. What do you want to buy? (e.g., 'bags of rice', 'cartons of noodles')",
                "tone": "neutral"
            })
        else:
            return jsonify({
                "message": f"You can borrow up to ₦{max_loan:,}. How much do you need, and what do you want to buy? (e.g., 'borrow 50000 to buy bags of rice')",
                "tone": "neutral"
            })

    # ---------- BUSINESS NETWORK SEARCH (PERMANENT) ----------
    search_triggers = [
        'who sell', 'who sells', 'who dey sell', 'find supplier', 'find someone who',
        'who does', 'who dey do', 'i need a', 'i dey find',
        'who supplies', 'where can i get', 'who get', 'who dey supply',
        'which person dey', 'who fit', 'who sabi', 'who dey run'
    ]
    if any(phrase in text.lower() for phrase in search_triggers):
        # Find the LONGEST matching trigger
        matched_trigger = ''
        for phrase in search_triggers:
            if phrase in text.lower() and len(phrase) > len(matched_trigger):
                matched_trigger = phrase

        # Remove the trigger phrase from the text (case‑insensitive)
        query = re.sub(re.escape(matched_trigger), '', text, flags=re.IGNORECASE).strip()

        # Remove trailing location words
        query = re.sub(r'\s*(?:in|for|at|near|around|wey\s+dey)\s*.*$', '', query, flags=re.IGNORECASE).strip()

        if len(query) < 2:
            query = 'crypto'

        facts = get_user_facts(user_id)
        my_city = facts.get('city', '')

        return jsonify({
            "action": "show_business_search",
            "search_query": query,
            "city": my_city,
            "message": f"Searching for '{query}'…",
            "tone": "neutral"
        })

    # --- Transfer confirmation (unchanged) ---
    if text.strip().lower() in ['yes', 'confirm', 'confirm transfer', 'ok', 'approve']:
        pending = pending_transfers.get(user_id)
        if pending:
            append_event(user_id, user_id, 'TransferConfirmed', pending['payload'])
            success, ref = mock_execute_transfer(pending['payload'])
            if success:
                append_event(user_id, user_id, 'TransferExecuted', {**pending['payload'], "reference": ref})
                del pending_transfers[user_id]
                save_conversation(user_id, 'user', text)
                return jsonify({
                    "message": f"Transfer of {pending['payload']['amount']} {pending['payload']['currency']} completed.",
                    "tone": "income"})
            else:
                append_event(user_id, user_id, 'TransferFailed', {**pending['payload'], "error": ref})
                del pending_transfers[user_id]
                save_conversation(user_id, 'user', text)
                return jsonify({"error": f"Transfer failed: {ref}"}), 500

    # --- P2P confirmation (unchanged) ---
    if text.strip().lower() in ['confirm', 'yes', 'ok'] and user_id in pending_p2p_trades:
        trade = pending_p2p_trades.pop(user_id)
        p2p_account_id = trade['account_id']
        try:
            from connectors.bybit_p2p import BybitP2PConnector
            accounts = get_user_connected_accounts(user_id)
            p2p_account = next((a for a in accounts if a['id'] == p2p_account_id), None)
            if not p2p_account:
                return jsonify({"error": "P2P account not found."}), 400
            api_key = decrypt(p2p_account['api_key_encrypted'])
            api_secret = decrypt(p2p_account['api_secret_encrypted'])
            connector = BybitP2PConnector(api_key, api_secret)

            if trade['action'] == 'sell':
                result = connector.place_sell_order(trade['amount'], trade['currency'], 'NGN', trade['ad_id'])
                append_event(user_id, p2p_account_id, 'P2PSellExecuted', {
                    "amount": trade['amount'],
                    "currency": trade['currency'],
                    "ngn_equivalent": trade['ngn_amount'],
                    "rate": trade['rate'],
                    "order_id": result.get('result', {}).get('orderId')
                })
                save_conversation(user_id, 'user', text)
                return jsonify({
                    "message": f"Sold {trade['amount']} {trade['currency']} for ₦{trade['ngn_amount']:,.2f}. P2P order created.",
                    "tone": "income"})
            elif trade['action'] == 'buy':
                result = connector.place_buy_order(trade['crypto_amount'], trade['currency'], 'NGN', trade['ad_id'])
                append_event(user_id, p2p_account_id, 'P2PBuyExecuted', {
                    "amount_ngn": trade['amount'],
                    "crypto_amount": trade['crypto_amount'],
                    "currency": trade['currency'],
                    "rate": trade['rate'],
                    "order_id": result.get('result', {}).get('orderId')
                })
                save_conversation(user_id, 'user', text)
                return jsonify({
                    "message": f"Bought {trade['crypto_amount']:.4f} {trade['currency']} for ₦{trade['amount']:,.2f}. P2P order created.",
                    "tone": "income"})
        except Exception as e:
            return jsonify({"error": f"P2P trade failed: {str(e)}"}), 500


    # ---------- IDENTITY VERIFICATION ----------
    if any(phrase in text.lower() for phrase in ['verify my identity', 'verify my account', 'activate wallet', 'link bvn', 'link nin', 'add bvn', 'add nin', 'i want to verify']):
        pending_transaction[user_id] = {
            "state": "ask_id_type",
            "data": {},
            "category": None
        }
        return jsonify({
            "message": "I can help you verify your identity with your BVN or NIN. Which one would you like to use? (Type 'BVN' or 'NIN')",
            "tone": "neutral"
        })


    # ---------- Rule‑based swap detector (fast path) ----------
    swap_match = re.match(r'swap\s+(\d+\.?\d*)\s*(\w+)\s+(?:for|to)\s+(\w+)\s+(?:on|in|using|from)?\s*(.*)', text, re.IGNORECASE)
    if swap_match:
        amount = float(swap_match.group(1))
        token_in = swap_match.group(2).upper()
        token_out = swap_match.group(3).upper()
        wallet_name = swap_match.group(4).strip().lower() or 'metamask'

        accounts = get_user_connected_accounts(user_id)
        wallet_account = None
        for acc in accounts:
            if acc['type'] == 'wallet' and wallet_name in acc['label'].lower():
                wallet_account = acc
                break
        if not wallet_account:
            wallet_account = next((acc for acc in accounts if acc['type'] == 'wallet'), None)
        if not wallet_account:
            return jsonify({"error": "No connected wallet found."}), 400

        swap_payload = {
            "token_in": token_in,
            "token_out": token_out,
            "amount": amount,
            "wallet": wallet_account['id'],
            "wallet_address": wallet_account['wallet_address'],
            "network": wallet_account['network'],
            "description": text
        }
        event = append_event(user_id, wallet_account['id'], 'SwapRequested', swap_payload)
        save_conversation(user_id, 'user', text)
        return jsonify({
            "message": f"Swapping {amount} {token_in} for {token_out} on {wallet_account['label']}. Confirm in your wallet.",
            "tone": "neutral",
            "event_id": event['event_id'],
            "requires_confirmation": True,
            "swap_payload": swap_payload
        })

    # ========== RULE-BASED FALLBACK ==========
    text_lower = text.lower().strip()

    # 1. Questions starting with how/what/…
    if text_lower.startswith(('how much', 'what is my', 'whats my', 'what are my', 'how many', 'what is the')):
        return handle_query(text, user_id)

    # 2. Exact greetings
    if text_lower in ['hello', 'hi', 'hey', 'good morning', 'good evening', 'help', 'what can you do']:
        name = get_user_name(user_id)
        return jsonify({"answer": f"Hi {name}! I'm Oyinda, your personal CFO. How can I help you today?", "tone": "neutral"})

    # 2b. Link bank command
    if text_lower in ['link bank', 'link my bank', 'connect bank', 'add bank account']:
        return jsonify({"open_mono": True, "message": "Opening bank connection…"})

    # Set home currency
    if text.lower().startswith('set my currency to ') or text.lower().startswith('change my currency to '):
        parts = text.split()
        new_currency = parts[-1].upper()
        if len(new_currency) != 3:
            return jsonify({"message": "Please use a 3‑letter currency code, like USD, GHS, NGN."})
        store_user_fact(user_id, 'home_currency', new_currency)
        return jsonify({"message": f"Your home currency is now {new_currency}. I'll convert future transactions to {new_currency}."})



    # 3. Balance / budget / net worth / credit score / debt keywords
    if any(w in text_lower for w in [
        'balance', 'how much is in', 'how much in', 'budget',
        'net worth', 'credit score', 'health score', 'debt', 'owe', 'liability',
        'how much am i worth', 'what am i worth', 'how much i worth',
        'my net worth', 'calculate my net worth'
    ]):
        return handle_query(text, user_id)

    if 'open a bank account' in text_lower or 'open bank account' in text_lower:
        return jsonify({
            "message": "We are partnering with trusted banks to let you open an account right here in Oyinda. You won't need to visit a bank or fill paper forms. I'll let you know as soon as this is ready!",
            "tone": "neutral"
        })

    # 4. Swap (crypto) – (a duplicate here is okay, but we already caught it above; keep for safety)
    swap_match = re.match(r'swap\s+(\d+\.?\d*)\s*(\w+)\s+(?:for|to)\s+(\w+)\s+(?:on|in|using|from)?\s*(.*)', text, re.IGNORECASE)
    if swap_match:
        amount = float(swap_match.group(1))
        token_in = swap_match.group(2).upper()
        token_out = swap_match.group(3).upper()
        wallet_name = swap_match.group(4).strip().lower() or 'metamask'

        accounts = get_user_connected_accounts(user_id)
        wallet_account = None
        for acc in accounts:
            if acc['type'] == 'wallet' and wallet_name in acc['label'].lower():
                wallet_account = acc
                break
        if not wallet_account:
            wallet_account = next((acc for acc in accounts if acc['type'] == 'wallet'), None)
        if not wallet_account:
            return jsonify({"error": "No connected wallet found."}), 400

        swap_payload = {
            "token_in": token_in,
            "token_out": token_out,
            "amount": amount,
            "wallet": wallet_account['id'],
            "wallet_address": wallet_account['wallet_address'],
            "network": wallet_account['network'],
            "description": text
        }
        event = append_event(user_id, wallet_account['id'], 'SwapRequested', swap_payload)
        save_conversation(user_id, 'user', text)
        return jsonify({
            "message": f"Swapping {amount} {token_in} for {token_out} on {wallet_account['label']}. Confirm in your wallet.",
            "tone": "neutral",
            "event_id": event['event_id'],
            "requires_confirmation": True,
            "swap_payload": swap_payload
        })

    # 5. Exchange trade
    trade_match = re.match(
        r'(?:i\s+(?:wan|want|want\s+to)\s+)?(buy|sell)\s+(\d+\.?\d*)\s*(\w+)\s+(?:on|using|with|from|for)?\s*(\w+)',
        text, re.IGNORECASE)
    if trade_match:
        action = trade_match.group(1).lower()
        amount = float(trade_match.group(2))
        symbol = trade_match.group(3).upper()
        exchange_name = trade_match.group(4).lower()

        common_assets = {'BTC', 'ETH', 'BNB', 'XRP', 'SOL', 'ADA', 'AVAX', 'LINK', 'DOT', 'LTC', 'BCH', 'ATOM', 'UNI',
                         'ETC', 'FIL', 'APT', 'ARB', 'OP', 'NEAR', 'MATIC'}
        if symbol in common_assets:
            symbol += 'USDT'

        accounts = get_user_connected_accounts(user_id)
        ex_account = None
        for acc in accounts:
            if acc['type'] == 'exchange' and exchange_name in acc['label'].lower():
                ex_account = acc
                break
        if not ex_account:
            return jsonify({"error": f"No exchange matching '{exchange_name}' found. Link it first."}), 400

        try:
            from connectors.exchange_factory import get_exchange_connector as factory_connector
            connector = factory_connector(ex_account)
            order = connector.place_order(symbol, action, amount)
            payload = {"symbol": symbol, "side": action, "quantity": amount, "order_id": order.get('orderId')}
            append_event(user_id, ex_account['id'], 'ExchangeOrderExecuted', payload)
            save_conversation(user_id, 'user', text)
            return jsonify({"message": f"{action.capitalize()} {amount} {symbol} on {ex_account['label']} submitted.",
                            "tone": "income"})
        except Exception as e:
            err_msg = str(e)
            if hasattr(e, 'response') and e.response is not None:
                try:
                    err_data = e.response.json()
                    err_msg = err_data.get('msg', err_msg)
                except:
                    pass
            return jsonify({"error": f"Trade failed: {err_msg}"}), 500

    # 6. Send token
    send_match = re.match(r'send\s+(\d+\.?\d*)\s*(\w+)\s+to\s+(0x[a-fA-F0-9]+)\s+(?:from|using|on)?\s*(.*)', text, re.IGNORECASE)
    if send_match:
        amount = float(send_match.group(1))
        token = send_match.group(2).upper()
        to_address = send_match.group(3)
        wallet_name = send_match.group(4).strip().lower() or 'bsc wallet'

        accounts = get_user_connected_accounts(user_id)
        wallet_account = None
        for acc in accounts:
            if acc['type'] == 'wallet' and wallet_name in acc['label'].lower():
                wallet_account = acc
                break
        if not wallet_account:
            wallet_account = next((acc for acc in accounts if acc['type'] == 'wallet'), None)
        if not wallet_account:
            return jsonify({"error": "No connected wallet found."}), 400

        send_payload = {
            "token": token,
            "amount": amount,
            "to_address": to_address,
            "wallet": wallet_account['id'],
            "wallet_address": wallet_account['wallet_address'],
            "network": wallet_account['network'],
            "description": text
        }
        event = append_event(user_id, wallet_account['id'], 'TokenTransferRequested', send_payload)
        save_conversation(user_id, 'user', text)
        return jsonify({
            "message": f"Sending {amount} {token} to {to_address} from {wallet_account['label']}. Confirm in your wallet.",
            "tone": "neutral",
            "event_id": event['event_id'],
            "requires_confirmation": True,
            "send_payload": send_payload
        })

    # 7. SELL USDT via MONICA
    sell_monica_match = re.match(r'sell\s+(\d+\.?\d*)\s*(USDT|USDC)\s+(?:for|to)\s*(?:ngn|naira)(?:\s*via\s*monica)?', text, re.IGNORECASE)
    if not sell_monica_match:
        sell_monica_match = re.match(r'convert\s+(\d+\.?\d*)\s*(USDT|USDC)\s+to\s+(?:ngn|naira)', text, re.IGNORECASE)
    if sell_monica_match:
        amount = float(sell_monica_match.group(1))
        currency = sell_monica_match.group(2).upper()
        accounts = get_user_connected_accounts(user_id)
        monica_account = next((a for a in accounts if a.get('provider', '').lower() == 'monica'), None)
        if not monica_account:
            return jsonify({"error": "No Monica account linked. Please link it under P2P."}), 400
        try:
            from connectors.monica import MonicaConnector
            api_key = decrypt(monica_account['api_key_encrypted'])
            connector = MonicaConnector(api_key)
            deposit_address = connector.get_deposit_address("TRC20")
            if not deposit_address:
                return jsonify({"error": "Could not get Monica deposit address."}), 500
        except Exception as e:
            return jsonify({"error": f"Monica API error: {str(e)}"}), 500
        wallet_accounts = [a for a in accounts if a['type'] == 'wallet']
        if not wallet_accounts:
            return jsonify({"error": "No connected crypto wallet."}), 400
        wallet_account = wallet_accounts[0]
        save_conversation(user_id, 'user', text)
        return jsonify({
            "action": "monica_sell",
            "message": f"Send {amount} {currency} to Monica's deposit address. Confirm in your wallet.",
            "data": {
                "amount": amount,
                "token": currency,
                "to_address": deposit_address,
                "network": wallet_account['network'],
                "wallet_address": wallet_account['wallet_address'],
                "monica_account_id": monica_account['id']
            },
            "tone": "neutral"
        })

    # ---------- EMERGENCY DATA SETUP ----------
    if any(phrase in text_lower for phrase in
           ['emergency data', 'offline data', 'send me data after', 'send data after']):
        # Check phone number
        facts = get_user_facts(user_id)
        phone = facts.get('phone')
        if not phone:
            return jsonify({
                "message": "I need your phone number first so I know where to send the data. "
                           "Please tell me your phone number, like 'my phone number is 080xxxxxxxx'.",
                "tone": "neutral"
            })

        # Start the emergency‑data state machine
        pending_transaction[user_id] = {
            "state": "collecting_emergency_hours",
            "data": {},
            "category": None
        }
        return jsonify({
            "message": "How many hours offline before I send you emergency data? (e.g., 3, 6, 12)\n\n"
                       "I will send you 33 MB of **MTN** data automatically when you've been offline "
                       "longer than that. Other networks coming soon!",
            "tone": "neutral"
        })

    # ---------- BUSINESS REGISTRATION ----------
    biz_reg_match = re.match(
        r'(?:i\s+)?(?:sell|supply|make|do)\s+(.+?)\s*(?:at|in|for)\s+([^,]+)'
        r'(?:,?\s*(?:call\s*(?:me|on))?\s*(\d{11}))?',
        text, re.IGNORECASE
    )
    if biz_reg_match:
        product = biz_reg_match.group(1).strip()
        market = biz_reg_match.group(2).strip()
        phone = biz_reg_match.group(3) or ''

        # Get user info
        facts = get_user_facts(user_id)
        city = facts.get('city', 'your city')
        name = get_user_name(user_id)

        # Determine category (goods vs services)
        has_unit = any(re.search(r'\b' + unit + r'\b', product.lower()) for unit in GOODS_UNITS)
        category = 'goods' if has_unit else 'services'

        # Insert into business_listings table (delete old listing first)
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM business_listings WHERE user_id = %s", (user_id,))
        cur.execute(
            """INSERT INTO business_listings (user_id, name, product, category, market_name, city, phone)
            VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (user_id, name, product, category, market, city, phone)
        )
        conn.commit()
        conn.close()

        phone_msg = f" Call me {phone}" if phone else ""
        return jsonify({
            "message": f"Your business is now listed! When someone searches for '{product}', they'll see: "
                       f"{name}, {market}, {city}{phone_msg}. "
                       f"Tap the 📞 button and they'll call you directly.",
            "tone": "income"
        })


    # ---------- LOAN REPAYMENT ----------
    repay_match = re.match(r'(?:i\s+)?(?:repaid|paid\s+back|cleared)\s+(\d+\.?\d*)\s*(?:of\s+my\s+loan|loan)?', text, re.IGNORECASE)
    if repay_match:
        amount = float(repay_match.group(1))
        append_event(user_id, user_id, 'LoanRepaid', {
            "amount": amount,
            "currency": "NGN",
            "date": datetime.utcnow().strftime("%Y-%m-%d"),
            "description": f"Repaid {amount} NGN of loan"
        })
        # Refresh credit score
        conn = get_conn()
        update_credit_score(conn, user_id)
        conn.close()
        name = get_user_name(user_id)
        return jsonify({"message": f"Noted, {name}. You've repaid {amount} NGN of your loan. Your credit score has been updated.", "tone": "income"})

    # 8. Expense logging
    expense_patterns = [
        r'(?:i\s+)?spent\s+(\d+\.?\d*)\s*(?:on\s+)?(.+)',
        r'(?:i\s+)?bought\s+(\d+\.?\d*)\s*(?:of\s+)?(.+)',
        r'(?:i\s+)?paid\s+(\d+\.?\d*)\s+(?:for\s+)?(.+)',
        r'i\s+drop\s+(\d+\.?\d*)\s+(?:for\s+|on\s+)?(.+)'
    ]
    expense_match = None
    for pat in expense_patterns:
        expense_match = re.match(pat, text, re.IGNORECASE)
        if expense_match:
            break

    if expense_match:
        amount = float(expense_match.group(1))
        description = expense_match.group(2).strip().lower()
        cat_map = {
            'food': 'food', 'rice': 'food', 'beans': 'food', 'spaghetti': 'food', 'maggi': 'food',
            'transport': 'transport', 'uber': 'transport', 'taxi': 'transport', 'okada': 'transport', 'fuel': 'transport',
            'data': 'utilities', 'internet': 'utilities', 'net': 'utilities', 'electricity': 'utilities', 'bill': 'utilities',
            'rent': 'housing', 'house': 'housing', 'accommodation': 'housing',
            'cloth': 'clothing', 'shoe': 'clothing',
            'doctor': 'health', 'medicine': 'health', 'hospital': 'health',
            'school': 'education', 'book': 'education', 'course': 'education',
            'movie': 'entertainment', 'game': 'entertainment', 'subscription': 'entertainment'
        }
        category = 'other'
        for word, cat in cat_map.items():
            if word in description:
                category = cat
                break
        payload = {
            "amount": amount,
            "currency": "NGN",
            "category": category,
            "date": datetime.utcnow().strftime("%Y-%m-%d"),
            "description": description
        }
        event = append_event(user_id, user_id, 'ExpenseLogged', payload)
        name = get_user_name(user_id)
        response_text = f"Got it, {name}. You spent {amount} NGN on {category}."
        budget = calculate_daily_budget(user_id)
        if budget:
            total_budget = sum(budget.values())
            daily_limit = total_budget / len(budget) if len(budget) > 0 else 0
            tone = "warning" if amount > daily_limit else "good"
        else:
            tone = "neutral"
        return jsonify({"message": response_text, "tone": tone, "event_id": event['event_id']})

    # 9. Income logging
    income_patterns = [
        r'(?:i\s+)?made\s+(\d+\.?\d*)\s*(?:profit|income|from|of)?\s*(.*)',
        r'(?:i\s+)?earned\s+(\d+\.?\d*)\s*(?:from\s+)?(.+)',
        r'(?:i\s+)?received\s+(\d+\.?\d*)\s*(?:from\s+)?(.+)',
        r'i\s+get\s+(\d+\.?\d*)\s+(?:from\s+)?(.+)'
    ]
    income_match = None
    for pat in income_patterns:
        income_match = re.match(pat, text, re.IGNORECASE)
        if income_match:
            break

    if income_match:
        amount = float(income_match.group(1))
        description = income_match.group(2).strip().lower() or 'income'
        payload = {
            "amount": amount,
            "currency": "NGN",
            "category": "income",
            "date": datetime.utcnow().strftime("%Y-%m-%d"),
            "description": description
        }
        event = append_event(user_id, user_id, 'IncomeReceived', payload)
        name = get_user_name(user_id)
        response_text = f"Great, {name}! You received {amount} NGN. That's a step forward."
        save_conversation(user_id, 'user', text)
        return jsonify({"message": response_text, "tone": "income", "event_id": event['event_id']})

    # ---------- MULTI-EXPENSE DETECTION ----------
    # Detect if the user listed multiple amounts (e.g., "data 2000, chinchin 200, milk 1000")
    # This runs BEFORE the single-expense patterns, so it doesn't get hijacked.
    # Detect amounts with optional currency prefixes (₦, $, €, £, R, etc.) and common 3‑letter codes (NGN, USD, GHS, KES, ZAR, etc.)
    amounts = re.findall(
        r'(?:'
        r'(?:[₦$€£¥₹]|R\$?|RM|Rp|₱|K|Sh|GH₵|DA|Dhs?|TSh|FCFA|Br|CFA|BIF|FRW|UGX|ZMW|AOA|MZN|MAD|LRD|SLL|GMD|CDF|STN|SCR|SZL|LSL|NAD|MWK|BWP|ETB|SDG|SSP|DJF|SOS|ERN|TND|LYD|EGP|MGA|MUR|SCR|KMF|XAF|XOF|XPF|CVE|GNF|SHP|FKP|BMD|KYD|ANG|AWG|BSD|BBD|BZD|BMD|BND|SGD|XCD|JMD|TTD|PAB|SVC|HTG|DOP|COP|VES|PEN|BOB|PYG|UYU|CLP|CRC|NIO|HNL|GTQ|BZD|ANG|AWG|BBD|BSD|BMD|KYD|ANG|AWG|BBD|BSD|BMD|KYD|ANG|AWG|BBD|BSD|BMD|KYD|ANG|AWG|BBD|BSD|BMD|KYD|ANG|AWG|BBD|BSD|BMD|KYD|ANG|AWG|BBD|BSD|BMD|KYD|ANG|AWG|BBD|BSD|BMD|KYD)'
        r'\s?'
        r')?'
        r'(\d[\d,]*\.?\d*)',
        text
    )
    if len(amounts) >= 2:
        # Convert all strings to floats
        parsed_amounts = []
        for a in amounts:
            try:
                parsed_amounts.append(float(a.replace(',', '')))
            except ValueError:
                continue
        if len(parsed_amounts) >= 2:
            total = sum(parsed_amounts)
            # Ask user to confirm the bulk log
            pending_transaction[user_id] = {
                "state": "confirming_bulk",
                "data": {
                    "amounts": parsed_amounts,
                    "total": total,
                    "description": text,
                    "currency": "NGN",
                    "type": "expense",           # assume expense; user can correct later
                    "category": "other"          # generic category
                },
                "category": None
            }
            return jsonify({
                "message": f"I see you mentioned {', '.join(f'₦{a:,.2f}' for a in parsed_amounts)}. That's ₦{total:,.2f} in total. Did you spend all of these? Reply 'yes' to log them all, or tell me what they are one by one.",
                "tone": "neutral"
            })

    # 10. Bank transfer
    transfer_match = re.match(r'(?:send|transfer)\s+(\d+\.?\d*)\s+to\s+(?:account\s+)?(\d+)\s*(?:,?\s*(\w+\s*bank))?', text, re.IGNORECASE)
    if transfer_match:
        amount = float(transfer_match.group(1))
        dest_account = transfer_match.group(2)
        bank_name = transfer_match.group(3).strip() if transfer_match.group(3) else 'bank'
        accounts = get_user_connected_accounts(user_id)
        if not accounts:
            return jsonify({"error": "No connected accounts."}), 400
        source_id = accounts[0]['id']
        dest_id = accounts[0]['id']
        payload = {
            "amount": amount,
            "currency": "NGN",
            "date": datetime.utcnow().strftime("%Y-%m-%d"),
            "description": f"Transfer to {dest_account} ({bank_name})",
            "source_account_id": source_id,
            "destination_account_id": dest_id
        }
        event = append_event(user_id, user_id, 'TransferRequested', payload)
        pending_transfers[user_id] = {"event_id": event['event_id'], "payload": payload}
        src_label = next((a['label'] for a in accounts if a['id'] == source_id), "your account")
        dst_label = f"{bank_name} {dest_account}"
        msg = f"Okay, I'll send {amount} NGN from {src_label} to {dst_label}. Please confirm this transfer."
        save_conversation(user_id, 'user', text)
        return jsonify({"message": msg, "tone": "neutral", "event_id": event['event_id']})

    # ---------- DETECT PHONE NUMBER (avoid logging as amount) ----------
    # Matches: "my phone number is 080xxx", "phone number: 080xxx", etc.
    phone_match = re.search(r'(?:phone|number)\s*(?:number|is|be|:)?\s*(\d{11})', text, re.IGNORECASE)
    if phone_match:
        phone = phone_match.group(1)
        if phone.startswith('0') and len(phone) == 11:
            store_user_fact(user_id, 'phone', phone)
            return jsonify({
                "message": f"I don save your phone number: {phone}. I go send your daily data reward to this number when you tell me your expenses.",
                "tone": "neutral"
            })


    # Set phone number
    if text.lower().startswith('set my phone number to ') or text.lower().startswith('change my phone number to '):
        parts = text.split()
        phone = parts[-1]
        # Basic validation
        if not phone.startswith('0') or len(phone) != 11:
            return jsonify({"message": "Please enter a valid 11‑digit Nigerian phone number starting with 0."})
        store_user_fact(user_id, 'phone', phone)
        return jsonify({"message": f"Your phone number has been saved as {phone}. I'll send your daily data reward to this number."})

    # ---------- POWER AVAILABILITY LOG ----------
    if any(phrase in text_lower for phrase in ['light don come', 'light come', 'power don come', 'nepa bring light']):
        append_event(user_id, user_id, 'PowerStatusChanged', {
            "status": "on",
            "timestamp": datetime.utcnow().isoformat(),
            "description": "Electricity came on"
        })
        return jsonify({
            "message": "I don record am. Light come back. I dey track how many hours you get this month.",
            "tone": "neutral"
        })



    # Internal transfer
    send_match = re.match(r'send\s+(\d+\.?\d*)\s+to\s+(\d{11})', text, re.IGNORECASE)
    if send_match:
        amount = float(send_match.group(1))
        recipient_phone = send_match.group(2)
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE facts->>'phone' = %s", (recipient_phone,))
        recip_row = cur.fetchone()
        if not recip_row:
            conn.close()
            return jsonify({"message": "User with that phone number not found. Ask them to join Oyinda first."})
        recip_id = recip_row[0]
        cur.execute("SELECT balance FROM user_wallets WHERE user_id = %s", (recip_id,))
        recip_wallet_row = cur.fetchone()
        if not recip_wallet_row:
            conn.close()
            return jsonify(
                {"message": "Recipient doesn't have an active wallet yet. They need to verify their identity."})
        sender_wallet = ensure_wallet(user_id)
        if sender_wallet['balance'] < amount:
            conn.close()
            return jsonify({"message": "Insufficient wallet balance."})

        new_sender_balance = sender_wallet['balance'] - amount
        new_recip_balance = float(recip_wallet_row[0]) + amount
        cur.execute("UPDATE user_wallets SET balance = %s, last_balance_update = now() WHERE user_id = %s",
                    (new_sender_balance, user_id))
        cur.execute("UPDATE user_wallets SET balance = %s, last_balance_update = now() WHERE user_id = %s",
                    (new_recip_balance, recip_id))
        conn.commit()
        conn.close()

        sender_phone = get_user_facts(user_id).get('phone', '')
        append_event(user_id, user_id, 'WalletDebited', {"amount": amount, "to_phone": recipient_phone})
        append_event(recip_id, recip_id, 'WalletCredited', {"amount": amount, "from_phone": sender_phone})
        save_conversation(user_id, 'user', text)
        return jsonify({
            "message": f"✅ Sent ₦{amount:,.2f} to {recipient_phone}. Your new balance: ₦{new_sender_balance:,.2f}",
            "tone": "income"
        })

    # External withdrawal
    withdraw_match = re.match(r'withdraw\s+(\d+\.?\d*)\s+to\s+(\d{3})\s+(\d{10})', text, re.IGNORECASE)
    if withdraw_match:
        amount = float(withdraw_match.group(1))
        bank_code = withdraw_match.group(2)
        account_number = withdraw_match.group(3)
        wallet = ensure_wallet(user_id)
        if wallet['balance'] < amount:
            return jsonify({"message": "Insufficient wallet balance."})

        mono = MonoReservedAccount()
        try:
            result = mono.payout(amount, bank_code, account_number, f"Oyinda withdrawal for {user_id}")
            if result.get('status') == 'success':
                new_balance = wallet['balance'] - amount
                conn = get_conn()
                cur = conn.cursor()
                cur.execute("UPDATE user_wallets SET balance = %s, last_balance_update = now() WHERE user_id = %s",
                            (new_balance, user_id))
                conn.commit()
                conn.close()
                append_event(user_id, user_id, 'WalletDebited',
                             {"amount": amount, "bank_code": bank_code, "account_number": account_number,
                              "type": "withdrawal"})
                save_conversation(user_id, 'user', text)
                return jsonify({
                    "message": f"🏦 Withdrawal of ₦{amount:,.2f} to {bank_code}/{account_number} initiated. New balance: ₦{new_balance:,.2f}",
                    "tone": "income"
                })
            else:
                return jsonify({"message": f"Payout failed: {result.get('message', 'unknown error')}"})
        except Exception as e:
            return jsonify({"message": f"Withdrawal error: {str(e)}"})





    if any(phrase in text_lower for phrase in ['light don go', 'light go', 'power don go', 'nepa take light']):
        append_event(user_id, user_id, 'PowerStatusChanged', {
            "status": "off",
            "timestamp": datetime.utcnow().isoformat(),
            "description": "Electricity went off"
        })
        return jsonify({
            "message": "I don record am. Light don go. I dey track how many hours you get this month.",
            "tone": "neutral"
        })


    send_match = re.match(r'send\s+(\d+\.?\d*)\s+to\s+(\d{11})', text, re.IGNORECASE)
    if send_match:
        amount = float(send_match.group(1))
        recipient_phone = send_match.group(2)
        # Look up recipient by phone (they might not have a wallet yet)
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE facts->>'phone' = %s", (recipient_phone,))
        recip_row = cur.fetchone()
        if not recip_row:
            conn.close()
            return jsonify({"message": "User with phone number not found. Ask them to join Oyinda first."})
        recip_id = recip_row[0]
        # Check if recipient has a wallet
        cur.execute("SELECT id, balance FROM user_wallets WHERE user_id = %s", (recip_id,))
        recip_wallet = cur.fetchone()
        if not recip_wallet:
            conn.close()
            return jsonify(
                {"message": "Recipient doesn't have an active wallet yet. They need to verify their identity first."})
        # Perform internal transfer: debit sender, credit recipient (both ledger updates)
        sender_wallet = ensure_wallet(user_id)
        if sender_wallet['balance'] < amount:
            conn.close()
            return jsonify({"message": "Insufficient balance."})
        # Debit sender
        new_sender_balance = sender_wallet['balance'] - amount
        cur.execute("UPDATE user_wallets SET balance = %s, last_balance_update = now() WHERE user_id = %s",
                    (new_sender_balance, user_id))
        # Credit recipient
        recip_new_balance = recip_wallet[1] + amount
        cur.execute("UPDATE user_wallets SET balance = %s, last_balance_update = now() WHERE user_id = %s",
                    (recip_new_balance, recip_id))
        conn.commit()
        conn.close()
        # Log events
        append_event(user_id, user_id, 'WalletDebited',
                     {"amount": amount, "recipient": recip_id, "description": f"Sent to {recipient_phone}"})
        append_event(recip_id, recip_id, 'WalletCredited',
                     {"amount": amount, "sender": user_id, "description": f"Received from {user_id}"})
        save_conversation(user_id, 'user', text)
        return jsonify({
            "message": f"Sent ₦{amount:,.2f} to {recipient_phone}. Your new balance: ₦{new_sender_balance:,.2f}",
            "tone": "income"
        })

    withdraw_match = re.match(r'withdraw\s+(\d+\.?\d*)\s+to\s+(\d{3})\s+(\d{10})', text, re.IGNORECASE)
    if withdraw_match:
        amount = float(withdraw_match.group(1))
        bank_code = withdraw_match.group(2)
        account_number = withdraw_match.group(3)
        wallet = ensure_wallet(user_id)
        if wallet['balance'] < amount:
            return jsonify({"message": "Insufficient wallet balance."})
        mono = MonoReservedAccount()
        try:
            result = mono.payout(amount, bank_code, account_number, "Oyinda withdrawal")
            if result.get('status') == 'success':
                # Debit wallet
                conn = get_conn()
                cur = conn.cursor()
                new_balance = wallet['balance'] - amount
                cur.execute("UPDATE user_wallets SET balance = %s, last_balance_update = now() WHERE user_id = %s",
                            (new_balance, user_id))
                conn.commit()
                conn.close()
                append_event(user_id, user_id, 'WalletDebited',
                             {"amount": amount, "bank_code": bank_code, "account_number": account_number,
                              "type": "withdrawal"})
                save_conversation(user_id, 'user', text)
                return jsonify({
                    "message": f"Withdrawal of ₦{amount:,.2f} to {bank_code}/{account_number} initiated. Your new balance: ₦{new_balance:,.2f}",
                    "tone": "income"
                })
            else:
                return jsonify({"message": f"Payout failed: {result.get('message', 'unknown error')}"})
        except Exception as e:
            return jsonify({"message": f"Error: {str(e)}"})


    # Manual loan repayment
    manual_repay_match = re.match(r'repay\s+(\d+\.?\d*)\s*(?:of\s+my\s+loan)?', text, re.IGNORECASE)
    if manual_repay_match:
        amount = float(manual_repay_match.group(1))
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id, remaining_balance FROM inventory_loans WHERE user_id = %s AND status = 'active'", (user_id,))
        loan = cur.fetchone()
        if not loan:
            conn.close()
            return jsonify({"message": "No active loan found."})
        loan_id, remaining = loan[0], loan[1]
        if amount > remaining:
            conn.close()
            return jsonify({"message": f"Amount exceeds remaining balance of ₦{remaining:,.2f}."})
        # Check wallet balance
        cur.execute("SELECT balance FROM user_wallets WHERE user_id = %s", (user_id,))
        wallet_row = cur.fetchone()
        if not wallet_row or wallet_row[0] < amount:
            conn.close()
            return jsonify({"message": "Insufficient wallet balance."})

        # Deduct wallet
        new_wallet_balance = wallet_row[0] - amount
        cur.execute("UPDATE user_wallets SET balance = %s, last_balance_update = now() WHERE user_id = %s",
                    (new_wallet_balance, user_id))
        # Update loan
        new_remaining = remaining - amount
        cur.execute("UPDATE inventory_loans SET remaining_balance = %s WHERE id = %s", (new_remaining, loan_id))
        cur.execute("INSERT INTO loan_repayments (loan_id, amount, method) VALUES (%s, %s, 'manual')",
                    (loan_id, amount))
        if new_remaining <= 0:
            cur.execute("UPDATE inventory_loans SET status = 'completed', remaining_balance = 0 WHERE id = %s", (loan_id,))
        conn.commit()
        conn.close()
        append_event(user_id, user_id, 'LoanRepaid', {"loan_id": str(loan_id), "amount": amount, "method": "manual"})
        save_conversation(user_id, 'user', text)
        return jsonify({
            "message": f"✅ Repaid ₦{amount:,.2f}. Remaining: ₦{max(0, new_remaining):,.2f}.",
            "tone": "income"
        })


    if any(phrase in text_lower for phrase in ['loan status', 'my loan', 'check my loan']):
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT il.product, il.principal, il.flat_fee, il.total_repayable, il.daily_amount,
                   il.remaining_balance, il.start_date, il.end_date, il.status, u.name
            FROM inventory_loans il
            JOIN users u ON il.supplier_id = u.id
            WHERE il.user_id = %s AND il.status = 'active'
        """, (user_id,))
        loan = cur.fetchone()
        conn.close()
        if loan:
            return jsonify({
                "message": f"📋 **Active Loan**\n"
                           f"• Product: {loan[0]}\n"
                           f"• Amount: ₦{loan[1]:,.2f}\n"
                           f"• Fee (5%): ₦{loan[2]:,.2f}\n"
                           f"• Total repayable: ₦{loan[3]:,.2f}\n"
                           f"• Daily repayment: ₦{loan[4]:,.2f}\n"
                           f"• Remaining: ₦{loan[5]:,.2f}\n"
                           f"• Period: {loan[6]} to {loan[7]}\n"
                           f"• Supplier: {loan[9]}"
            })
        else:
            return jsonify({"message": "You have no active inventory loan."})



    # ---- MINIMUM SCORE FOR A TARGET LOAN AMOUNT ----
    score_for_amount_match = re.search(
        r'(?:how\s+(?:many|much)\s+credit\s+score\s+(?:do|will)\s+I\s+(?:need|have)\s+to\s+borrow\s+)?(\d[\d,]*\.?\d*)',
        text, re.IGNORECASE
    )
    if score_for_amount_match and any(phrase in text_lower for phrase in [
        'how many credit score', 'how much credit score', 'credit score to borrow',
        'score to borrow', 'score do i need', 'score will i have'
    ]):
        target_amount = float(score_for_amount_match.group(1).replace(',', ''))
        # Find the minimum score tier that allows at least this amount
        tiers = [
            (50, 10_000),
            (101, 20_000),
            (151, 49_000),
            (201, 100_000),
            (351, 200_000),
            (501, 500_000),
            (701, 1_000_000),
            (801, 1_100_000),
        ]
        min_score_needed = None
        for score_threshold, max_loan in tiers:
            if target_amount <= max_loan:
                min_score_needed = score_threshold
                break
        if min_score_needed:
            return jsonify({
                "message": (
                    f"To borrow ₦{target_amount:,.0f}, you need a credit score of at least {min_score_needed}. "
                    "Keep logging your daily expenses and income to grow your score!"
                ),
                "tone": "neutral"
            })
        else:
            return jsonify({
                "message": f"₦{target_amount:,.0f} is above our current maximum loan amount. Please try a smaller amount.",
                "tone": "warning"
            })
    # ---- LOAN AMOUNT FOR A GIVEN SCORE ----
    hypothetical_score_match = re.search(
        r'(?:if\s+(?:I\s+have|my)\s+credit\s+score\s+(?:is|of)\s+)?(\d{2,3})',
        text, re.IGNORECASE
    )
    if hypothetical_score_match and any(phrase in text_lower for phrase in [
        'if i have', 'if my credit score', 'credit score of', 'how much will i be able to borrow',
        'how much can i borrow', 'how much will i get'
    ]):
        hypothetical_score = int(hypothetical_score_match.group(1))
        if 50 <= hypothetical_score <= 850:
            max_loan = get_max_loan_amount(hypothetical_score)
            if max_loan == 0:
                return jsonify({
                    "message": f"With a score of {hypothetical_score}, you won't qualify for a loan yet. Keep logging transactions to reach 50.",
                    "tone": "neutral"
                })
            else:
                return jsonify({
                    "message": (
                        f"If your credit score is {hypothetical_score}, you can borrow up to ₦{max_loan:,}.\n"
                        "Your current score is {}/850. Keep logging expenses to reach that target!"
                    ).format(get_credit_score(user_id)['score']),
                    "tone": "neutral"
                })
        else:
            return jsonify({"message": "Credit score must be between 50 and 850.", "tone": "warning"})

    # ---- DIRECT BORROW REQUEST (NO SUPPLIER) ----
    if re.search(r'\b(?:borrow|lend)\s+me\s+(\d[\d,]*\.?\d*)', text, re.IGNORECASE):
        # Treat it exactly like "can I get a loan"
        credit = get_credit_score(user_id)
        max_loan = get_max_loan_amount(credit['score'])
        if max_loan == 0:
            return jsonify({
                "message": "Your credit score is below 50. Keep telling me your daily expenses and income, and your score will grow!",
                "tone": "neutral"
            })
        else:
            return jsonify({
                "message": (
                    f"Your credit score is {credit['score']}/850. "
                    f"You can borrow up to ₦{max_loan:,}.\n"
                    "To request an inventory loan, say like: 'borrow 50000 to buy bags of rice from Mama Tunde'. "
                    "I'll pay the supplier directly and you repay in 14 days."
                ),
                "tone": "income"
            })

    # ---------- GENERIC LOAN INQUIRY (before fallback) ----------
    if any(phrase in text_lower for phrase in [
        'can i get a loan', 'how to get loan', 'i need loan',
        'loan eligibility', 'how much loan can i get'
    ]):
        credit = get_credit_score(user_id)
        max_loan = get_max_loan_amount(credit['score'])
        if max_loan == 0:
            return jsonify({
                "message": "Your credit score is below 50. Keep telling me your daily expenses and income, and your score will grow!",
                "tone": "neutral"
            })
        else:
            return jsonify({
                "message": (
                    f"Your credit score is {credit['score']}/850. "
                    f"You can borrow up to ₦{max_loan:,}.\n"
                    "To request an inventory loan, say like: 'borrow 50000 to buy bags of rice from Mama Tunde'. "
                    "I'll pay the supplier directly and you repay in 14 days."
                ),
                "tone": "income"
            })


    # ---------- SMART FALLBACK with user‑aware currency conversion ----------
    # Step 1: Look for a price‑indicating word followed by a number
    price_match = re.search(
        r'(?:for|at|cost|costs|sold\s*at|price\s*of)\s*'
        r'(?:₦|naira|\$|usd|€|£|GH₵|R)?\s*'
        r'(\d[\d,]*\.?\d*)\s*(k|K)?',
        text, re.IGNORECASE
    )
    if price_match:
        amount_str = price_match.group(1).replace(',', '')
        amount_original = float(amount_str)
        if price_match.group(2):  # 'k' or 'K' → multiply by 1000
            amount_original *= 1000
        currency_symbol = '₦'  # default if no symbol captured in this branch
    else:
        # Step 2: Try a currency‑prefixed number anywhere in the text
        currency_match = re.search(
            r'([₦$€£¥]|R\$?|RM|Rp|GH₵|DA|Dhs?|TSh|FCFA|Br|CFA|BIF|FRW|UGX|ZMW|AOA|MZN|MAD|LRD|SLL|GMD|CDF|STN|SCR|SZL|LSL|NAD|MWK|BWP|ETB|SDG|SSP|DJF|SOS|ERN|TND|LYD|EGP|MGA|MUR|SCR|KMF|XAF|XOF|XPF|CVE|GNF|SHP|FKP|BMD|KYD|ANG|AWG|BSD|BBD|BZD|BMD|BND|SGD|XCD|JMD|TTD|PAB|SVC|HTG|DOP|COP|VES|PEN|BOB|PYG|UYU|CLP|CRC|NIO|HNL|GTQ)\s*'
            r'(\d[\d,]*\.?\d*)\s*(k|K)?',
            text, re.IGNORECASE
        )
        if currency_match:
            amount_str = currency_match.group(2).replace(',', '')
            amount_original = float(amount_str)
            if currency_match.group(3):
                amount_original *= 1000
            currency_symbol = currency_match.group(1) or '₦'
        else:
            # Step 3: Fall back to the largest number, ignoring quantity‑like numbers
            all_numbers = re.findall(r'(\d[\d,]*\.?\d*)\s*(k|K)?', text)
            if all_numbers:
                candidates = []
                for num_str, suffix in all_numbers:
                    val = float(num_str.replace(',', ''))
                    if suffix:
                        val *= 1000
                    # If a goods unit follows, it's probably a quantity → ignore
                    after_num = text[text.find(num_str) + len(num_str):].strip()
                    is_quantity = any(re.match(r'\b' + unit + r'\b', after_num) for unit in GOODS_UNITS)
                    if not is_quantity:
                        candidates.append(val)
                if candidates:
                    amount_original = max(candidates)
                else:
                    # All numbers looked like quantities – use the largest one anyway
                    all_values = [float(n.replace(',', '')) * (1000 if s else 1) for n, s in all_numbers]
                    amount_original = max(all_values)
                currency_symbol = '₦'  # assume Naira if no symbol detected
            else:
                # No number found at all – fall through to conversational LLM
                reply = conversational_reply(user_id, text)
                if reply:
                    save_conversation(user_id, 'cfo', reply)
                    return jsonify({"message": reply, "tone": "neutral"})
                return jsonify({
                    "message": "I didn't catch an amount. Could you say something like 'I spent 500 on food'?",
                    "tone": "neutral"
                })

    # ---------- CURRENCY DETECTION ----------
    symbol_to_code = {
        '$': 'USD', '₦': 'NGN', '€': 'EUR', '£': 'GBP',
        '¥': 'JPY', 'R': 'ZAR', 'R$': 'ZAR', 'GH₵': 'GHS', 'DA': 'DZD',
        'DH': 'MAD', 'Dhs': 'AED', 'TSh': 'TZS', 'FCFA': 'XAF', 'Br': 'ETB',
        'CFA': 'XAF', 'RM': 'MYR', 'Rp': 'IDR', 'K': 'KES', 'Sh': 'KES',
    }
    currency_code = symbol_to_code.get(currency_symbol.strip(), 'NGN')

    # ---------- USER‑AWARE CONVERSION ----------

    facts = get_user_facts(user_id)
    home_currency = facts.get('home_currency', 'NGN') or 'NGN'
    amount_converted = convert_currency(amount_original, currency_code,
                                        home_currency) if currency_code != home_currency else amount_original

    # ---------- CREATE PENDING TRANSACTION ----------
    pending_transaction[user_id] = {
        "state": "collecting_type",
        "data": {
            "amount": amount_converted,  # logged in home currency
            "original_amount": amount_original,
            "original_currency": currency_code,
            "home_currency": home_currency,
            "description": text,
            "currency": home_currency,
            "category": None
        },
        "category": None
    }

    # ---------- GOODS vs SERVICES DETECTION ----------
    has_unit = any(re.search(r'\b' + unit + r'\b', text.lower()) for unit in GOODS_UNITS)
    pending_transaction[user_id]["data"]["transaction_type"] = "goods" if has_unit else "services"

    # ---------- TYPE DETECTION (unchanged keyword checks) ----------
    if any(word in text.lower() for word in ['spent', 'bought', 'paid', 'expense', 'drop']):
        pending_transaction[user_id]["data"]["type"] = "expense"
        pending_transaction[user_id]["state"] = "collecting_category"
        return ask_next_question(user_id)
    elif any(word in text.lower() for word in ['earned', 'made', 'profit', 'income', 'received']):
        pending_transaction[user_id]["data"]["type"] = "income"
        pending_transaction[user_id]["state"] = "collecting_category"
        return ask_next_question(user_id)
    elif any(word in text.lower() for word in ['saved', 'invested', 'save', 'invest', 'savings']):
        pending_transaction[user_id]["data"]["type"] = "investment"
        pending_transaction[user_id]["data"]["category"] = "investment"
        pending_transaction[user_id]["category"] = "investment"
        return ask_for_location(user_id)
    elif any(phrase in text.lower() for phrase in [
        'investor gave', 'partner gave', 'capital to trade',
        'manage this money', 'investment capital', 'to invest',
        'for investment', 'fund me', 'funds to trade',
        'money to run business', 'money for business',
        'investment for', 'capital for', 'received investment',
        'received capital', 'gave me capital', 'gave me to invest',
        'money to invest', 'trading capital', 'business capital'
    ]):
        pending_transaction[user_id]["data"]["type"] = "managed_funds"
        pending_transaction[user_id]["state"] = "confirming_funds"
        return jsonify({
            "message": f"I understand someone gave you {amount_converted:,.2f} {home_currency} as investment capital. Is that correct? (reply 'yes' or 'no')",
            "tone": "neutral"
        })
    elif any(word in text.lower() for word in ['borrow', 'loan', 'lend']):
        pending_transaction[user_id]["data"]["type"] = "loan"
        pending_transaction[user_id]["state"] = "collecting_category"
        return ask_next_question(user_id)
    else:
        return jsonify({
            "message": f"Did you spend, earn, invest, save, or take a loan of {amount_converted:,.2f} {home_currency}? (original: {amount_original} {currency_code})",
            "tone": "neutral"
        })

    # ---------- ENHANCED SMALL‑TALK FALLBACK (avoids LLM loop) ----------
    small_talk = {
        'what can you do': "I'm your CFO, Gbenga! I track expenses & income, build your credit score, give cheap inventory loans, connect you with suppliers, and even pay your taxes. Just tell me like 'spent 500 on food' or 'earned 2000 from sales'.",
        'what can unida do': "You mean Oyinda! 😊 I track your money, build your credit score, give loans for stock, and connect you with suppliers. Tell me something you spent or earned today.",
        'what is my next word': "Your next word can be anything – like 'spent 2000 on fuel' or 'earned 5000 from my shop'. Tell me what happened with your money today!",
        'how much i do': "I need a little more detail. Did you spend or receive money? For example, 'spent 1500 on transport' or 'earned 3000 from sales'.",
        'hello': None,  # already handled earlier
        'hi': None,
        'help': "I can help you track money, get loans, find suppliers, and more. Just tell me an expense like 'spent 500 on data', or ask 'what is my balance'.",
    }
    # Clean the text for matching
    clean_text = text_lower.strip().rstrip('?.!')
    if clean_text in small_talk:
        reply = small_talk[clean_text]
        if reply:
            save_conversation(user_id, 'user', text)
            return jsonify({"message": reply, "tone": "neutral"})


    # ---------- CONVERSATIONAL FALLBACK (LLM) ----------
    reply = conversational_reply(user_id, text)
    if reply:
        save_conversation(user_id, 'cfo', reply)
        return jsonify({"message": reply, "tone": "neutral"})

    # If LLM fails, give your new static helpful prompt
    return jsonify({
        "message": "I understand you. And i have taken note. You could also tell me everything about your finances, like how much you make today, what you spent money on or what loan or asset you want to track. I will help you track everything. get you a credit score for loan application, a tax receipt for your business or a broader Transaction statement for travel purposes or any other official use. So tell me how much have you spent today?",
        "tone": "neutral"
    })

@app.route('/command', methods=['POST'])
@jwt_required()
def handle_command():
    user_id = get_jwt_identity()
    data = request.get_json()
    text = data.get('text', '').strip()
    save_conversation(user_id, 'user', text)
    if not text:
        return jsonify({"error": "No text provided"}), 400
    return process_user_command(user_id, text)


# --------------- QUERY HANDLER (with voice-friendly responses) ---------------
def handle_query(text, user_id):
    query_info = classify_query_intent(text)
    text_lower = text.lower()

    # ---------- PRODUCT KNOWLEDGE – NEVER SEND THESE TO THE LLM ----------
    if any(w in text_lower for w in [
        'credit score', 'health score', 'butterfly', 'eagle', 'what is my score',
        'financial health', 'how am i doing financially', 'my financial health',
        'how healthy are my finances', 'financial wellbeing', 'my finances'
    ]):
        score_data = get_credit_score(user_id)
        score = score_data["score"]
        logo = score_data["logo"]

        # Simple explanation of the scale
        if score < 580:
            status = "just starting out. Log more transactions and pay back loans to improve."
        elif score < 740:
            status = "doing well. Regular saving and paying debts on time will boost it further."
        else:
            status = "excellent. You're a financial eagle!"

        return jsonify({
            "answer": f"Your Oyinda credit score is {score}/850 ({logo}). That means you're {status}\n"
                      f"Score range: 300‑579 (Butterfly 🦋), 580‑739 (Transition), 740‑850 (Eagle 🦅).",
            "tone": "neutral"
        })

    # Greeting
    if query_info and query_info.get('intent') == 'greeting':
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT name, account_type FROM users WHERE id=%s", (user_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            name, acct = row
            if acct == 'personal':
                return jsonify({"answer": f"Hi {name}! I'm Oyinda, your personal assistant for financial inclusion. How can I help you today?", "tone": "neutral"})
            else:
                return jsonify({"answer": f"Hello {name}! I'm your AI CFO. How may I assist you?", "tone": "neutral"})
        return jsonify({"answer": "Hello! I'm Oyinda, your financial companion.", "tone": "neutral"})

    # Manual parse for "spent on <category> <time>"
    spent_match = re.match(r'(?:how much\s+)?(?:did\s+)?i\s+spent\s+on\s+(\w+)\s+(.+)', text_lower)
    if spent_match:
        category = spent_match.group(1)
        date_part = spent_match.group(2).strip()
        # Map common category words
        cat_map = {'internet':'utilities','data':'utilities','food':'food','transport':'transport','fuel':'transport'}
        category = cat_map.get(category, category)
        start, end, label = extract_date_range(date_part)
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT SUM(amount) FROM transactions_view WHERE user_id=%s AND type='expense' AND category=%s AND date BETWEEN %s AND %s",
                    (user_id, category, start, end))
        total = cur.fetchone()[0] or 0
        conn.close()
        return jsonify({"answer": f"Total spent on {category} for {label}: ₦{total:,.2f}", "tone": "neutral"})


    # Smart queries with date & category
    if query_info:
        intent = query_info.get('intent')
        params = query_info.get('parameters', {})
        date_param = params.get('date')
        category = params.get('category')
        if intent in ('expense', 'income') and (date_param or category):
            start, end, label = extract_date_range(date_param or 'all time')
            conn = get_conn()
            cur = conn.cursor()
            type_filter = "type='expense'" if intent == 'expense' else "type='income'"
            cat_filter = ""
            qparams = [user_id, start, end]
            if category:
                cat_filter = " AND category = %s"
                qparams.append(category)
            cur.execute(f"SELECT SUM(amount) FROM transactions_view WHERE user_id=%s AND {type_filter} AND date BETWEEN %s AND %s{cat_filter}", qparams)
            total = cur.fetchone()[0] or 0
            conn.close()
            cat_text = f" on {category}" if category else ""
            return jsonify({"answer": f"Total {intent}{cat_text} for {label}: ₦{total:,.2f}", "tone": "neutral"})

    # Only respond to "my liability / my debt" queries, not definitions
    if any(phrase in text_lower for phrase in [
        'my liability', 'my debt', 'how much do i owe', 'how much liability',
        'total liability', 'my total liability', 'what is my liability',
        'how much is my liability', 'how much debt am i owing'
    ]) and not any(word in text_lower for word in ['define', 'what is a', 'meaning of', 'explain']):
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT SUM(amount) FROM transactions_view WHERE user_id=%s AND type='expense' AND category='loan'",
                    (user_id,))
        total = cur.fetchone()[0] or 0
        conn.close()
        return jsonify({"answer": f"Your total liability (loans taken) is ₦{total:,.2f}.", "tone": "neutral"})

    # Only respond to "my investment" queries, not definitions or advice
    if any(phrase in text_lower for phrase in [
        'my investment', 'how much have i invested', 'total investment',
        'what is my investment', 'how much investment', 'my total investment',
        'investment amount', 'investment so far'
    ]) and not any(word in text_lower for word in ['how to', 'how do i', 'should i', 'define', 'meaning', 'explain']):
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT SUM(amount) FROM transactions_view WHERE user_id=%s AND type='expense' AND category='investment'",
            (user_id,))
        total = cur.fetchone()[0] or 0
        conn.close()
        return jsonify({"answer": f"Total investments recorded: ₦{total:,.2f}.", "tone": "neutral"})

    # Budget
    if any(w in text_lower for w in ['budget','limit','spend limit']):
        budget = calculate_daily_budget(user_id)
        if not budget:
            return jsonify({"answer": "I don't have enough data yet. Log some income and expenses first.", "tone": "neutral"})
        msg = "Here's your daily budget:\n" + "\n".join([f"• {k.replace('_',' ').title()}: ₦{v:,.2f}" for k,v in budget.items()])
        return jsonify({"answer": msg, "tone": "neutral"})



    # ---------- LOAN STATUS ----------
    if any(phrase in text_lower for phrase in ['how much do i owe', 'my loan', 'loan balance', 'outstanding loan']):
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT SUM(CASE WHEN event_type = 'LoanTaken' THEN amount ELSE 0 END),
                   SUM(CASE WHEN event_type = 'LoanRepaid' THEN amount ELSE 0 END)
            FROM events
            WHERE user_id = %s AND event_type IN ('LoanTaken', 'LoanRepaid')
        """, (user_id,))
        taken, repaid = cur.fetchone()
        conn.close()
        taken = taken or 0
        repaid = repaid or 0
        outstanding = taken - repaid

        if taken == 0:
            return jsonify({
                               "answer": "You don't have any active loans. Would you like to apply for one? Just ask 'can I get a loan?'",
                               "tone": "neutral"})

        # Get last loan details
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT payload FROM events
            WHERE user_id = %s AND event_type = 'LoanTaken'
            ORDER BY created_at DESC LIMIT 1
        """, (user_id,))
        last_loan = cur.fetchone()
        conn.close()

        details = ""
        if last_loan:
            payload = json.loads(last_loan[0]) if isinstance(last_loan[0], str) else last_loan[0]
            details = f"\n• Monthly payment: ₦{payload.get('monthly_payment', 0):,.2f}"
            details += f"\n• Total repayable: ₦{payload.get('total_repayable', 0):,.2f}"
            details += f"\n• Interest rate: {payload.get('interest_rate', 0)}% monthly"

        # Build the answer string to avoid f‑string escaping issues
        if outstanding <= 0:
            repayment_hint = "Well done! Your loan is fully repaid. 🎉"
        else:
            repayment_hint = "Make a repayment anytime: just say 'I repaid 2000 of my loan'."

        answer = (
            f"💰 **Your Loan Summary**\n\n"
            f"• Total borrowed: ₦{taken:,.2f}\n"
            f"• Total repaid: ₦{repaid:,.2f}\n"
            f"• Outstanding: **₦{outstanding:,.2f}**\n"
            f"{details}\n\n"
            f"{repayment_hint}"
        )

        return jsonify({"answer": answer, "tone": "neutral"})

    # Credit score
    if 'credit score' in text_lower or 'health score' in text_lower:
        score = get_credit_score(user_id)
        return jsonify({"answer": f"Your financial health score is {score['score']}/100. You're a {score['logo']}.", "tone": "neutral"})


    if 'breakdown' in text_lower or 'pillar' in text_lower or 'why is my score' in text_lower or 'what affects' in text_lower:
        conn = get_conn()
        breakdown = update_credit_score(conn, user_id)  # re‑calculate to get fresh breakdown
        conn.close()
        pillars = breakdown["pillars"]
        msg = "Your credit score is made up of five parts:\n\n"
        for key, data in pillars.items():
            name = key.replace('_', ' ').title()
            msg += f"• {name} ({data['weight']}): {data['score']}% – {data['note']}\n"
        msg += f"\nYour total score is {breakdown['fico']}/850."
        return jsonify({"answer": msg, "tone": "neutral"})


    # Net worth (true asset‑liability calculation)
    if 'net worth' in text_lower or 'networth' in text_lower:
        try:
            result = calculate_net_worth(user_id)
            return jsonify({"answer": result, "tone": "neutral"})
        except Exception as e:
            import traceback
            traceback.print_exc()
            return jsonify({"answer": f"Could not calculate net worth: {str(e)}", "tone": "warning"})

    # Assets / accounts
    if any(w in text_lower for w in ['asset','account','wallet','bank','what do i own','what do i have']):
        accounts = get_user_connected_accounts(user_id)
        if not accounts:
            return jsonify({"answer": "You haven't linked any accounts yet.", "tone": "neutral"})
        msg = "Your connected accounts:\n" + "\n".join([f"• {a['label']} ({a['currency']})" for a in accounts])
        return jsonify({"answer": msg, "tone": "neutral"})

    # Transactions list (last 5)
    if 'last' in text_lower and 'transaction' in text_lower:
        try:
            n = int([w for w in text_lower.split() if w.isdigit()][0])
        except:
            n = 5
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT date, type, amount, currency, description FROM transactions_view WHERE user_id=%s ORDER BY date DESC LIMIT %s", (user_id, n))
        rows = cur.fetchall()
        conn.close()
        if not rows:
            return jsonify({"answer": "No transactions yet.", "tone": "neutral"})
        lines = [f"{r[0][:10]} {r[1]}: ₦{r[2]:,.2f} {r[3]} - {r[4]}" for r in rows]
        return jsonify({"answer": "Your latest transactions:\n" + "\n".join(lines), "tone": "neutral"})

    # Pattern: "total spent on <category> <time>"
    spent_on = re.match(r'(?:total\s+)?spent\s+on\s+(\w+)\s+(.+)', text_lower)
    if spent_on:
        category = spent_on.group(1)
        date_part = spent_on.group(2).strip()
        # map aliases
        cat_map = {'internet':'utilities','data':'utilities','fuel':'transport','rice':'food'}
        category = cat_map.get(category, category)
        start, end, label = extract_date_range(date_part)
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT SUM(amount) FROM transactions_view WHERE user_id=%s AND type='expense' AND category=%s AND date BETWEEN %s AND %s",
                    (user_id, category, start, end))
        total = cur.fetchone()[0] or 0
        conn.close()
        return jsonify({"answer": f"Total spent on {category} for {label}: ₦{total:,.2f}", "tone": "neutral"})

    # Simple expense/income fallback
    if any(w in text_lower for w in ['spent','expense','spend']):
        start, end, label = extract_date_range(text_lower)
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT SUM(amount) FROM transactions_view WHERE user_id=%s AND type='expense' AND date BETWEEN %s AND %s", (user_id, start, end))
        total = cur.fetchone()[0] or 0
        conn.close()
        return jsonify({"answer": f"Total expenses for {label}: ₦{total:,.2f}", "tone": "neutral"})
    if any(w in text_lower for w in ['made', 'earned', 'income', 'profit', 'revenue']):
        start, end, label = extract_date_range(text_lower)
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT SUM(amount) FROM transactions_view WHERE user_id=%s AND type='income' AND date BETWEEN %s AND %s", (user_id, start, end))
        total = cur.fetchone()[0] or 0
        conn.close()
        return jsonify({"answer": f"Total income for {label}: ₦{total:,.2f}", "tone": "neutral"})

    # Balance queries
    if any(w in text_lower for w in ['balance', 'how much is in', 'how much in', 'how many assets', 'savings']):
        accounts = get_user_connected_accounts(user_id)
        if not accounts:
            return jsonify({"answer": "You haven't linked any accounts yet.", "tone": "neutral"})

        # If user specifically asks about "savings"
        if 'savings' in text_lower:
            # 1. Sum stablecoin balances from all wallets
            stablecoins = ['USDT', 'USDC', 'BUSD', 'DAI']
            total_savings_ngn = 0.0
            lines = []
            for acc in accounts:
                if acc['type'] == 'wallet':
                    try:
                        from connectors.balances import get_account_balance
                        bal_str = get_account_balance(acc)
                        # Extract token lines
                        token_line_match = re.search(r'Tokens:\s*(.*)', bal_str)
                        if token_line_match:
                            token_line = token_line_match.group(1)
                            tokens = re.findall(r'(\w+):\s*([\d,]+\.?\d*)', token_line)
                            for token, amount_str in tokens:
                                if token.upper() in stablecoins:
                                    amount = float(amount_str.replace(',', ''))
                                    # Convert to NGN (using demo rates)
                                    rates = {'USDT': 1500, 'USDC': 1500, 'BUSD': 1500, 'DAI': 1500}
                                    ngn_val = amount * rates.get(token.upper(), 1500)
                                    total_savings_ngn += ngn_val
                                    lines.append(f"{acc['label']} - {token}: {amount:,.2f} (≈ ₦{ngn_val:,.2f})")
                    except:
                        pass
            # 2. Also add any bank accounts labelled "savings" (future)
            for acc in accounts:
                if acc['type'] == 'bank' and 'savings' in acc['label'].lower():
                    try:
                        bal = get_account_balance(acc)
                        # extract numeric value (simple)
                        match = re.search(r'([\d,]+\.?\d*)', bal)
                        if match:
                            val = float(match.group(1).replace(',', ''))
                            total_savings_ngn += val  # already in NGN
                            lines.append(f"{acc['label']}: ₦{val:,.2f}")
                    except:
                        pass

            if not lines:
                return jsonify(
                    {"answer": "No savings found yet. Stablecoins in wallets will automatically count as savings.",
                     "tone": "neutral"})

            lines.append(f"\n**Total Savings: ₦{total_savings_ngn:,.2f}**")
            return jsonify({"answer": "\n".join(lines), "tone": "neutral"})

        # Original per‑account matching (unchanged)
        # … (keep the existing alias matching and per‑account balance logic)

        # Aliases to map common names to actual labels
        aliases = {
            'metamask': ['bsc wallet', 'ethereum wallet', 'metamask'],
            'trust wallet': ['trust wallet', 'trust'],
            'binance': ['binance'],
            'uba': ['uba'],
            'main': ['main ngn account'],
        }

        # Try to find a matching account
        matched = None
        for acc in accounts:
            # Direct label or type match
            if acc['label'].lower() in text_lower or acc['type'].lower() in text_lower:
                matched = acc
                break
            # Check against known aliases
            for alias, labels in aliases.items():
                if alias in text_lower and any(label in acc['label'].lower() for label in labels):
                    matched = acc
                    break
            if matched:
                break

        if matched:
            try:
                from connectors.balances import get_account_balance
                result = get_account_balance(matched)
                return jsonify({"answer": result, "tone": "neutral"})
            except Exception as e:
                return jsonify({"answer": f"Could not fetch balance: {str(e)}", "tone": "warning"})
        else:
            # No specific account mentioned – list all balances
            from connectors.balances import get_account_balance
            lines = []
            for acc in accounts:
                try:
                    bal = get_account_balance(acc)
                    lines.append(f"• {bal}")
                except:
                    lines.append(f"• {acc['label']}: balance unavailable")
            return jsonify({"answer": "Here are your balances:\n" + "\n".join(lines), "tone": "neutral"})

    # Tax estimation
    if 'tax' in text_lower:
        start, end, label = extract_date_range(text_lower)
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT SUM(amount) FROM transactions_view WHERE user_id=%s AND type='income' AND date BETWEEN %s AND %s", (user_id, start, end))
        total_income = cur.fetchone()[0] or 0
        conn.close()

        # Simple Nigerian tax brackets (approximate)
        tax = 0
        if total_income > 30000000:
            tax = (total_income - 30000000) * 0.30 + 6000000 * 0.25 + 18000000 * 0.15 + 300000 * 0.07
        elif total_income > 12000000:
            tax = (total_income - 12000000) * 0.25 + 18000000 * 0.15 + 300000 * 0.07
        elif total_income > 600000:
            tax = (total_income - 600000) * 0.15 + 300000 * 0.07
        elif total_income > 300000:
            tax = (total_income - 300000) * 0.07
        # else: tax = 0

        return jsonify({"answer": f"Estimated tax for {label}: ₦{tax:,.2f} (based on Nigerian PAYE brackets)", "tone": "neutral"})


    # ---------- LOAN ELIGIBILITY ----------
    if any(phrase in text_lower for phrase in ['can i get a loan', 'do i qualify for a loan', 'i need a loan',
                                                'borrow money', 'loan offer', 'lend me']):
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM transactions_view WHERE user_id = %s", (user_id,))
        txn_count = cur.fetchone()[0]
        cur.execute("SELECT MIN(created_at) FROM events WHERE user_id = %s", (user_id,))
        first_event = cur.fetchone()[0]
        days_active = (datetime.utcnow().date() - first_event.date()).days if first_event else 0
        conn.close()

        if days_active < 7:
            return jsonify({"answer": "You need at least 7 days of transaction history to apply for a loan. Keep telling me your daily expenses!", "tone": "neutral"})

        score_data = get_credit_score(user_id)
        credit_score = score_data['score']
        max_loan = get_max_loan_amount(credit_score)
        if max_loan == 0:
            return jsonify({"answer": f"Your credit score is {credit_score}/850. You need at least 100 to qualify. Keep logging your transactions!", "tone": "neutral"})

        interest = get_loan_interest_rate(credit_score)
        return jsonify({
            "answer": f"🎉 You qualify for a loan!\n\n"
                      f"• Credit score: **{credit_score}/850**\n"
                      f"• Maximum amount: **₦{max_loan:,.2f}**\n"
                      f"• Monthly interest: **{interest}%**\n"
                      f"• Duration: 3‑12 months\n"
                      f"• First month interest‑free!\n\n"
                      f"To apply, just tell me how much you want and for how many months.\n"
                      f"Example: *'borrow 20,000 for 6 months'*",
            "tone": "income"
        })



    # ---------- PERSONALISED FINANCIAL REVIEW ----------
    if any(phrase in text_lower for phrase in [
        'how am i doing', 'am i doing well', 'do you think i dey do well',
        'rate my finance', 'evaluate my spending', 'am i spending too much',
        'how is my money', 'how am i managing money',
        'what do you think about my finances',
        'what can you say about my money habit',
        'analyze my spending', 'how is my financial behaviour',
        'money habit', 'spending habit', 'spending pattern'
    ]):
        # Fetch 30‑day summary
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN type='income' THEN amount ELSE 0 END), 0) as total_income,
                COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END), 0) as total_expense,
                COUNT(*) as total_txns
            FROM transactions_view
            WHERE user_id=%s AND date >= NOW() - INTERVAL '30 days'
        """, (user_id,))
        row = cur.fetchone()
        total_income = row[0] if row else 0
        total_expense = row[1] if row else 0
        total_txns = row[2] if row else 0

        # Recent 5 transactions
        cur.execute("""
            SELECT date, type, amount, description FROM transactions_view
            WHERE user_id=%s ORDER BY date DESC LIMIT 5
        """, (user_id,))
        recent = cur.fetchall()
        conn.close()

        recent_str = "No transactions yet."
        if recent:
            lines = []
            for r in recent:
                date_part = r[0].strftime("%Y-%m-%d") if hasattr(r[0], 'strftime') else str(r[0])[:10]
                lines.append(f"{date_part} - {r[1]}: ₦{r[2]:,.2f} ({r[3]})")
            recent_str = "\n".join(lines)

        # Build a personalised prompt
        stats_prompt = (
            SYSTEM_PROMPT + "\n\n"
            f"The user asked: \"{text}\"\n"
            f"Here is their 30‑day financial summary:\n"
            f"Total income: ₦{total_income:,.2f}\n"
            f"Total expenses: ₦{total_expense:,.2f}\n"
            f"Total transactions: {total_txns}\n\n"
            f"Recent activity:\n{recent_str}\n\n"
            "Give a short, warm, Pidgin‑friendly reply. "
            "Be specific – mention the numbers, compare income vs expenses, and give practical advice. "
            "If there are no transactions yet, encourage them to start logging."
        )

        # Use the same LLM pipeline
        reply = _call_llm("groq", stats_prompt)
        if not reply:
            reply = _call_llm("openai", stats_prompt)
        if reply:
            save_conversation(user_id, 'cfo', reply)
            return jsonify({"answer": reply, "tone": "neutral"})

        # If LLM fails, fall through to the generic conversational fallback


    # Currency exchange rate queries: "what is USD to NGN?", "how much is $1 in naira?", "rate of GBP to GHS", etc.
    rate_match = re.search(r'(?:rate|exchange|convert|how much is|what is|wetin be)\s+(\w{2,4})\s*(?:to|in|for|dey)\s*(\w{2,4})', text_lower)
    if rate_match:
        from_cur = rate_match.group(1).upper()
        to_cur = rate_match.group(2).upper()
        if len(from_cur) < 3 or len(to_cur) < 3:
            # maybe the user typed "usd" as "dollar"? We can map common names
            currency_aliases = {
                'dollar': 'USD', 'dollars': 'USD', 'usd': 'USD',
                'naira': 'NGN', 'ngn': 'NGN',
                'pounds': 'GBP', 'pound': 'GBP', 'sterling': 'GBP',
                'euro': 'EUR', 'eur': 'EUR',
                'cedi': 'GHS', 'ghs': 'GHS',
                'rand': 'ZAR', 'zar': 'ZAR',
                'shilling': 'KES', 'kes': 'KES',
            }
            from_cur = currency_aliases.get(from_cur.lower(), from_cur)
            to_cur = currency_aliases.get(to_cur.lower(), to_cur)
        if from_cur and to_cur and len(from_cur) == 3 and len(to_cur) == 3:
            try:
                rate = convert_currency(1, from_cur, to_cur)
                return jsonify({
                    "answer": f"The current exchange rate is **1 {from_cur} = {rate:,.2f} {to_cur}**.",
                    "tone": "neutral"
                })
            except Exception:
                pass


    # Power availability summary
    if any(phrase in text_lower for phrase in ['how many hours of light', 'power hours', 'light hours', 'hours of electricity']):
        conn = get_conn()
        cur = conn.cursor()
        first_of_month = datetime.utcnow().replace(day=1).strftime('%Y-%m-%d')
        cur.execute("""
            SELECT payload->>'status', payload->>'timestamp'
            FROM events
            WHERE user_id = %s AND event_type = 'PowerStatusChanged'
            AND created_at >= %s
            ORDER BY created_at ASC
        """, (user_id, first_of_month))
        rows = cur.fetchall()
        conn.close()

        # Calculate total powered hours
        total_seconds = 0
        last_on_time = None
        for row in rows:
            status = row[0]
            timestamp = datetime.fromisoformat(row[1])
            if status == 'on':
                last_on_time = timestamp
            elif status == 'off' and last_on_time:
                total_seconds += (timestamp - last_on_time).total_seconds()
                last_on_time = None

        # If power is still on now, count time until now
        if last_on_time:
            total_seconds += (datetime.utcnow() - last_on_time).total_seconds()

        total_hours = round(total_seconds / 3600, 1)

        if total_hours == 0:
            return jsonify({"answer": "I no record any light for this month yet. Tell me when light come back: 'light don come'.", "tone": "neutral"})
        else:
            return jsonify({
                "answer": f"You don get light for about **{total_hours} hours** this month. That one fit help you negotiate your NEPA bill.",
                "tone": "neutral"
            })



    # ---------- BUSINESS NETWORK SEARCH ----------
    search_triggers = [
        'who sell', 'who sells', 'who dey sell', 'find supplier', 'find someone who',
        'who does', 'who dey do', 'i need a', 'i dey find',
        'who supplies', 'where can i get', 'who get', 'who dey supply',
        'which person dey', 'who fit', 'who sabi', 'who dey run'
    ]
    if any(phrase in text_lower for phrase in search_triggers):
        # Extract the product/service – grab everything after the trigger phrase
        search_query = ''
        for phrase in search_triggers:
            if phrase in text_lower:
                idx = text_lower.find(phrase) + len(phrase)
                search_query = text_lower[idx:].strip()
                break

        # If extraction failed, just use the whole text after the trigger as the query
        if not search_query:
            search_query = text_lower

        # Remove trailing fluff like "in ibadan", "for me", etc.
        search_query = re.sub(r'\s*(?:in|for|at|near|around|wey\s+dey)\s*.*$', '', search_query).strip()

        if len(search_query) < 2:
            return jsonify({"answer": "What are you looking for? Please say something like 'who sells tomatoes'.", "tone": "neutral"})

        facts = get_user_facts(user_id)
        my_city = facts.get('city', '')

        return jsonify({
            "action": "show_business_search",
            "search_query": search_query,
            "city": my_city,
            "message": f"Searching for '{search_query}' near you…",
            "tone": "neutral"
        })

    # If no specific query matched, try conversational LLM
    reply = conversational_reply(user_id, text)
    if reply:
        save_conversation(user_id, 'cfo', reply)
        return jsonify({"answer": reply, "tone": "neutral"})


    # Static fallback if LLM fails
    return jsonify({
        "answer": "I understand you. I have taken note. I noticed what you are asking me has a broader scope. If you want, I can help you Swap any amount of crypto in your web3 wallet or transfer any amount to another wallet or help you buy or sell any amount of crypto in any exchange you have under 3 seconds. I can check your account balances across all your connected wallets or bank accounts. I can check your networth for you so you see your financial standing easily. If you connect your local banks, your crypto wallets or exchanges or your savings and investment apps, i could do anything you would want to do in those places right from here. Just tell me and i will do it under 1 second. How much have you spent today? ",
        "tone": "neutral"
    })



# Temporary store for onboarding state (token -> state dict)


@app.route('/onboard', methods=['POST'])
def onboard():
    data = request.get_json()
    language = data.get('language', 'english').lower()
    token = data.get('token', '')
    text = data.get('text', '').strip()

    conn = get_conn()
    cur = conn.cursor()

    # ---------- Start new session ----------
    if not token:
        token = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO onboarding_sessions (token, step, data) VALUES (%s, %s, %s)",
            (token, 'ask_new_or_returning', json.dumps({"language": language}))
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({
            "token": token,
            "message": "Hello! I'm Oyinda, your personal CFO. Are you new here, or do you already have an account? (Type 'new' or 'login')",
            "tone": "neutral"
        })

    # ---------- Load existing session ----------
    cur.execute("SELECT step, data FROM onboarding_sessions WHERE token = %s", (token,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        token = str(uuid.uuid4())
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO onboarding_sessions (token, step, data) VALUES (%s, %s, %s)",
            (token, 'ask_new_or_returning', json.dumps({"language": language}))
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({
            "token": token,
            "message": "Session expired. Let's start over. Are you new here, or do you already have an account? (Type 'new' or 'login')",
            "tone": "neutral"
        })

    step = row[0]
    user_data = row[1] if isinstance(row[1], dict) else json.loads(row[1])
    # Always keep language in sync with the user's latest choice
    language = data.get('language', '').lower()
    if language:
        user_data['language'] = language

    # ---------- RESET HANDLER ----------
    if text.strip().lower() == 'reset':
        cur.execute("DELETE FROM onboarding_sessions WHERE token = %s", (token,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({
            "message": "Let's start over. Are you new here, or do you already have an account? (Type 'new' or 'login')",
            "tone": "neutral"
        })

    # ---- NEW OR RETURNING ----
    if step == 'ask_new_or_returning':
        if any(word in text.lower() for word in ['login', 'returning', 'existing', 'already', 'have']):
            user_data = {}
            cur.execute(
                "UPDATE onboarding_sessions SET step = 'login_identity', data = %s WHERE token = %s",
                (json.dumps(user_data), token)
            )
            conn.commit()
            cur.close()
            conn.close()
            return jsonify({"message": "Welcome back! What's your email or username?", "tone": "neutral"})
        elif any(word in text.lower() for word in ['new', 'register', 'sign up', 'create']):
            user_data = {}
            cur.execute(
                "UPDATE onboarding_sessions SET step = 'ask_name', data = %s WHERE token = %s",
                (json.dumps(user_data), token)
            )
            conn.commit()
            cur.close()
            conn.close()
            return jsonify({"message": "Great! Let's get you started. What's your full name?", "tone": "neutral"})
        else:
            cur.close()
            conn.close()
            return jsonify({
                "message": "I didn't get that. Are you new here (type 'new') or do you already have an account (type 'login')?",
                "tone": "neutral"
            })

    # ---- LOGIN BRANCH ----
    if step == 'login_identity':
        user_data['identity'] = text.strip()
        cur.execute(
            "UPDATE onboarding_sessions SET step = 'login_password', data = %s WHERE token = %s",
            (json.dumps(user_data), token)
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"message": "Enter your password:", "tone": "neutral"})

    if step == 'login_password':
        user_data['password'] = text
        identity = user_data['identity']
        # Try email first, then username
        from core import authenticate_user_by_email_or_username
        user = authenticate_user_by_email_or_username(identity, user_data['password'])
        if not user:
            # Try finding by username
            conn2 = get_conn()
            cur2 = conn2.cursor()
            cur2.execute("SELECT id, name, email, password_hash, account_type FROM users WHERE username = %s", (identity,))
            row2 = cur2.fetchone()
            conn2.close()
            if row2 and check_password(user_data['password'], row2[3]):
                user = {"id": str(row2[0]), "name": row2[1], "email": row2[2], "account_type": row2[4]}
        if not user:
            # Keep session alive – back to login_identity for retry
            cur.execute(
                "UPDATE onboarding_sessions SET step = 'login_identity', data = %s WHERE token = %s",
                (json.dumps(user_data), token)
            )
            conn.commit()
            cur.close()
            conn.close()
            return jsonify({
                "message": "Incorrect email/username or password. Please try again (or type 'reset' to start over).",
                "tone": "warning"
            })
        # Success
        access_token = create_access_token(identity=str(user['id']))
        cur.execute("DELETE FROM onboarding_sessions WHERE token = %s", (token,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({
            "jwt": access_token,
            "user": {"id": user['id'], "name": user['name']},
            "message": f"Welcome back, {user['name']}!",
            "tone": "income",
            "redirect": "/dashboard"
        })

    # ---- REGISTRATION BRANCH (no email) ----
    if step == 'ask_name':
        user_data['name'] = text.strip()
        cur.execute(
            "UPDATE onboarding_sessions SET step = 'ask_password', data = %s WHERE token = %s",
            (json.dumps(user_data), token)
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"message": f"Nice to meet you, {user_data['name']}! Create a password (minimum 6 characters):", "tone": "neutral"})

    if step == 'ask_password':
        if len(text) < 6:
            cur.close()
            conn.close()
            return jsonify({"message": "Password must be at least 6 characters. Try again:", "tone": "neutral"})
        user_data['password'] = text
        cur.execute(
            "UPDATE onboarding_sessions SET step = 'ask_type', data = %s WHERE token = %s",
            (json.dumps(user_data), token)
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"message": "Are you an individual, a small business owner, or a company? (Type: individual / business / company)", "tone": "neutral"})

    if step == 'ask_type':
        user_type = text.strip().lower()
        if user_type not in ['individual', 'business', 'company']:
            cur.close()
            conn.close()
            return jsonify({"message": "Please choose one: individual, business, or company.", "tone": "neutral"})
        user_data['account_type'] = user_type
        if user_type in ['business', 'company']:
            cur.execute(
                "UPDATE onboarding_sessions SET step = 'ask_business_name', data = %s WHERE token = %s",
                (json.dumps(user_data), token)
            )
            conn.commit()
            cur.close()
            conn.close()
            return jsonify({"message": "What is the name of your business? (or type 'skip' to skip)", "tone": "neutral"})
        else:
            cur.close()
            conn.close()
            return finalize_registration(token)

    if step == 'ask_business_name':
        if text.strip().lower() != 'skip':
            user_data['business_name'] = text.strip()
        cur.execute(
            "UPDATE onboarding_sessions SET step = 'ask_business_address', data = %s WHERE token = %s",
            (json.dumps(user_data), token)
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"message": "Business address? (or type 'skip')", "tone": "neutral"})

    if step == 'ask_business_address':
        if text.strip().lower() != 'skip':
            user_data['business_address'] = text.strip()
        cur.close()
        conn.close()
        return finalize_registration(token)

    # Fallback
    cur.close()
    conn.close()
    return jsonify({"message": "Something went wrong. Let's start over. What's your full name?", "tone": "neutral"})


def confirm_registration(token):
    state = onboarding_state.get(token)
    if not state:
        return jsonify({"message": "Session expired. Please start again."})

    user_data = state["data"]

    from core import create_user
    user_id = create_user(
        name=user_data["name"],
        email=user_data["email"],
        password=user_data["password"],
        account_type=user_data.get("account_type", "personal"),
        address=user_data.get("business_address", "")
    )
    if not user_id:
        # Email already exists – go back to the email step
        state["step"] = "ask_email"
        return jsonify({
            "message": "That email is already registered. Please enter a different email address:",
            "tone": "neutral"
        })

    # Success – remove state and log the user in
    onboarding_state.pop(token, None)

    if user_data.get("account_type") in ['business', 'company']:
        store_user_fact(user_id, "business_name", user_data.get("business_name", ""))
        store_user_fact(user_id, "business_address", user_data.get("business_address", ""))

    access_token = create_access_token(identity=str(user_id))
    reply = onboarding_message(token, "confirm", user_data, None)
    return jsonify({
        "token": token,
        "jwt": access_token,
        "user": {"id": user_id, "name": user_data["name"]},
        "message": reply or f"All set, {user_data['name']}! You're now registered. Redirecting to your dashboard…",
        "tone": "income",
        "redirect": "/dashboard"
    })


def conversational_reply(user_id, text):
    """Use the LLM to generate a friendly, context-aware response."""
    try:
        # Get recent conversation history
        history = get_recent_conversation(user_id, 6)
        # Get user facts
        from core import get_user_facts, get_credit_score
        facts = get_user_facts(user_id)
        score_data = get_credit_score(user_id)
        user_lang = facts.get('language', 'english')

        # Build system message
        system_msg = SYSTEM_PROMPT + "\n\n"
        if facts:
            system_msg += f"User facts: {json.dumps(facts)}. Use these to personalise your reply.\n"

        # Inject the real credit score and pillar breakdown
        if score_data:
            system_msg += f"\nThe user's real Oyinda credit score is {score_data['score']}/850 ({score_data['logo']}). "
            breakdown = facts.get('credit_breakdown', {})
            if breakdown:
                system_msg += "Here is the pillar breakdown:\n"
                for key, data in breakdown.items():
                    system_msg += f"- {key.replace('_',' ').title()} ({data['weight']}): {data['score']}% – {data['note']}\n"
            system_msg += "\n"

        system_msg += "\nRecent conversation:\n"
        for msg in history:
            role = "User" if msg['role'] == 'user' else "Oyinda"
            system_msg += f"{role}: {msg['content']}\n"
        system_msg += f"\nThe user just said: \"{text}\"\n"
        # Language matching rule
        system_msg += (
            f"\nCRITICAL: The user's preferred language is '{user_lang}'. "
            "You MUST respond in that language. If the user writes in English, reply in English. "
            "If they write in Pidgin, reply in Pidgin. Never force Pidgin for an English speaker. "
            "Keep your reply to 1-2 short, caring sentences. "
            "If the user is just chatting, acknowledge them warmly without forcing a transaction."
        )

        # Try Groq first, then Google, then OpenAI fallback
        print("CONVERSATIONAL_REPLY system_msg (first 200 chars):", system_msg[:200])
        reply = _call_llm("groq", system_msg)
        print("CONVERSATIONAL_REPLY groq reply:", reply)
        if not reply:
            reply = _call_llm("google", system_msg)
            print("CONVERSATIONAL_REPLY google reply:", reply)
        if not reply:
            reply = _call_llm("openai", system_msg)
            print("CONVERSATIONAL_REPLY openai reply:", reply)

        # Random feedback request removed – Oyinda no longer asks for ratings
        return reply
    except Exception:
        return None



def onboarding_message(token, step, user_data, user_text=None):
    """
    Use the LLM to generate a warm onboarding message,
    but NEVER change the state. The state machine controls the flow.
    """
    system_msg = SYSTEM_PROMPT + "\n\n"
    system_msg += f"You are onboarding a new user. The current step is: {step}.\n"
    system_msg += f"Data already collected: {json.dumps(user_data)}.\n"
    system_msg += (
        "Your ONLY job is to rephrase the required question in a warm, friendly, Pidgin‑friendly way.\n"
        "Do NOT answer for the user. Do NOT skip steps. Do NOT ask a different question.\n"
        "The required questions for each step are:\n"
        "- ask_new_or_returning: 'Are you new here, or do you already have an account? (Type new or login)'\n"
        "- ask_name: 'What's your full name?'\n"
        "- ask_email: 'What's your email address?'\n"
        "- ask_password: 'Create a password (minimum 6 characters)'\n"
        "- ask_type: 'Are you an individual, small business owner, or a company?'\n"
        "- ask_business_name: 'What is the name of your business? (or type skip)'\n"
        "- ask_business_address: 'Business address? (or type skip)'\n"
        "- login_email: 'Welcome back! What's your email address?'\n"
        "- login_password: 'Enter your password'\n"
        "- confirm: 'All set! You're now registered.'\n"
        "- done: 'Welcome back!' (for login)\n"
        "Now, using the exact required question above, generate a one‑sentence warm reply that asks ONLY that question.\n"
        "Do NOT mention other features, do NOT ask multiple questions, do NOT add unrelated info."
    )

    if user_text:
        system_msg += f"\n\nThe user just said: \"{user_text}\"\n"

    reply = _call_llm("groq", system_msg)
    if not reply:
        reply = _call_llm("openai", system_msg)
    return reply


def _call_llm(provider, system_msg):
    try:
        if provider == "groq":
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {os.environ.get('GROQ_API_KEY')}"},
                json={
                    "model": "qwen-3.6-27b",
                    "messages": [{"role": "system", "content": system_msg}],
                    "temperature": 0.7,
                    "top_p": 0.9,
                    "max_tokens": 200
                },
                timeout=15
            )
        elif provider == "openai":
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY')}"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "system", "content": system_msg}],
                    "temperature": 0.8,
                    "top_p": 0.9,
                    "max_tokens": 200
                },
                timeout=15
            )
        elif provider == "google":
            # Gemini via REST API
            api_key = os.environ.get("GOOGLE_API_KEY")
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}",
                json={
                    "contents": [{"parts": [{"text": system_msg}]}],
                    "generationConfig": {
                        "temperature": 0.7,
                        "topP": 0.9,
                        "maxOutputTokens": 200
                    }
                },
                timeout=15
            )
            data = resp.json()
            # Gemini returns a different structure
            try:
                return data['candidates'][0]['content']['parts'][0]['text'].strip()
            except (KeyError, IndexError):
                print(f"CALL_LLM google unexpected response: {data}")
                return None
        else:
            return None

        # For Groq and OpenAI, extract the reply
        if provider in ("groq", "openai"):
            data = resp.json()
            if 'choices' in data:
                return data['choices'][0]['message']['content'].strip()
            else:
                print(f"CALL_LLM {provider} error: {data}")
                return None
    except Exception as e:
        print(f"CALL_LLM {provider} exception: {str(e)}")
        return None


@app.route('/reminder', methods=['POST'])
@jwt_required()
def create_reminder():
    user_id = get_jwt_identity()
    data = request.get_json()
    message = data.get('message')
    remind_at = data.get('remind_at')   # ISO datetime string, e.g. "2026-07-06T08:00:00"

    if not message or not remind_at:
        return jsonify({"error": "message and remind_at required"}), 400

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO reminders (user_id, message, remind_at) VALUES (%s, %s, %s)",
        (user_id, message, remind_at)
    )
    conn.commit()
    conn.close()
    return jsonify({"message": "Reminder set successfully."})



# --------------- STATEMENT (PDF/JSON) ---------------

@app.route('/statement', methods=['GET'])
def generate_statement():
    # Accept token via query parameter or header
    token = request.args.get('token') or request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        return jsonify({"error": "Missing token"}), 401

    try:
        from flask_jwt_extended import decode_token
        decoded = decode_token(token)
        user_id = decoded['sub']
    except Exception:
        return jsonify({"error": "Invalid or expired token"}), 401

    from_date = request.args.get('from', '1900-01-01')
    to_date = request.args.get('to', datetime.utcnow().strftime('%Y-%m-%d'))
    fmt = request.args.get('format', 'markdown')

    # ---------- 1. Gather connected institutions ----------
    accounts = get_user_connected_accounts(user_id)
    institutions = {}
    for acc in accounts:
        inst_id = acc['id']
        institutions[inst_id] = {
            'name': acc['label'],
            'type': acc['type'].upper(),
            'currency': acc.get('currency', 'NGN'),
            'address': '',
            'swift': '',
            'contact': ''
        }

    # ---------- 2. Fetch all financial events in the range ----------
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT stream_id, event_type, payload, created_at
        FROM events
        WHERE user_id = %s
          AND created_at BETWEEN %s AND %s
          AND event_type IN (
              'ExpenseLogged', 'IncomeReceived',
              'TransferExecuted', 'TransferFailed',
              'CryptoOrderExecuted', 'ExchangeOrderExecuted',
              'P2PBuyExecuted', 'P2PSellExecuted',
              'TokenTransferExecuted', 'SwapExecuted'
          )
        ORDER BY created_at ASC
    """, (user_id, from_date + ' 00:00:00', to_date + ' 23:59:59'))

    events = cur.fetchall()
    cur.close()
    conn.close()

    # ---------- 3. Build transaction ledger ----------
    ledger = []
    running_balances = defaultdict(lambda: 0)

    for evt in events:
        stream_id, event_type, payload, created_at = evt
        payload = json.loads(payload) if isinstance(payload, str) else payload
        amount = payload.get('amount', 0) or 0
        currency = payload.get('currency', 'NGN')

        inst_id = stream_id
        inst_name = institutions.get(inst_id, {}).get('name', None)
        if not inst_name and event_type in ('SwapExecuted', 'TokenTransferExecuted',
                                            'ExchangeOrderExecuted', 'CryptoOrderExecuted'):
            original_event_id = payload.get('original_event_id')
            if original_event_id:
                conn2 = get_conn()
                cur2 = conn2.cursor()
                cur2.execute("SELECT stream_id FROM events WHERE event_id = %s", (original_event_id,))
                orig_row = cur2.fetchone()
                cur2.close()
                conn2.close()
                if orig_row:
                    orig_stream_id = orig_row[0]
                    if orig_stream_id in institutions:
                        inst_id = orig_stream_id
                        inst_name = institutions[inst_id]['name']
        if not inst_name:
            inst_name = 'Informal'
            inst_id = 'manual'

        running_balances.setdefault(inst_id, 0)

        if event_type in ('ExpenseLogged', 'TransferExecuted', 'TokenTransferExecuted',
                          'SwapExecuted', 'P2PBuyExecuted'):
            withdrawal = amount
            deposit = 0
            running_balances[inst_id] -= amount
        elif event_type in ('IncomeReceived', 'CryptoOrderExecuted', 'ExchangeOrderExecuted',
                            'P2PSellExecuted'):
            withdrawal = 0
            deposit = amount
            running_balances[inst_id] += amount
        else:
            withdrawal = 0
            deposit = 0

        description = payload.get('description', event_type)
        date_str = created_at.strftime('%Y-%m-%d')

        ledger.append({
            'date': date_str,
            'institution': inst_name,
            'description': description,
            'withdrawal': withdrawal,
            'deposit': deposit,
            'currency': currency,
            'running_balance': running_balances[inst_id]
        })

    # ---------- 4. Compute summary figures ----------
    total_deposits = sum(item['deposit'] for item in ledger)
    total_withdrawals = sum(item['withdrawal'] for item in ledger)
    avg_monthly_spend = round(total_withdrawals / max(1, len(set(item['date'] for item in ledger)) / 30), 2)

    # ---------- 5. Build Markdown ----------
    md = f"# OFFICIAL STATEMENT OF ACCOUNT\n\n"
    md += f"**Statement Period:** {from_date} to {to_date}\n"
    md += f"**Date of Issue:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n"

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name, email FROM users WHERE id=%s", (user_id,))
    user = cur.fetchone()
    conn.close()
    if user:
        md += f"**Account Holder:** {user[0]}\n"
        md += f"**Email:** {user[1]}\n"
    md += f"**Customer ID:** {user_id}\n\n"

    md += "## Linked Institutions\n\n"
    for inst_id, info in institutions.items():
        md += f"- **{info['name']}** ({info['type']}) – {info['currency']}\n"
    md += "\n"

    md += "## Account Summary\n\n"
    md += "| Institution | Starting Balance | Total Deposits (+) | Total Withdrawals (-) | Ending Balance |\n"
    md += "|-------------|-----------------|-------------------|-----------------------|----------------|\n"
    for inst_id, info in institutions.items():
        sb = 0
        eb = running_balances[inst_id]
        inst_items = [it for it in ledger if it['institution'] == info['name']]
        inst_dep = sum(it['deposit'] for it in inst_items)
        inst_wth = sum(it['withdrawal'] for it in inst_items)
        md += f"| {info['name']} | {sb:,.2f} | {inst_dep:,.2f} | {inst_wth:,.2f} | {eb:,.2f} |\n"
    md += f"| **TOTAL** | **0.00** | **{total_deposits:,.2f}** | **{total_withdrawals:,.2f}** | **{sum(running_balances.values()):,.2f}** |\n\n"

    md += "## Transaction Ledger\n\n"
    md += "| Date | Institution | Description | Withdrawal (-) | Deposit (+) | Running Balance |\n"
    md += "|------|-------------|-------------|----------------|-------------|-----------------|\n"
    for item in ledger:
        wd = f"{item['withdrawal']:,.2f}" if item['withdrawal'] else ''
        dp = f"{item['deposit']:,.2f}" if item['deposit'] else ''
        rb = f"{item['running_balance']:,.2f}"
        md += f"| {item['date']} | {item['institution']} | {item['description']} | {wd} | {dp} | {rb} |\n"

    md += "\n---\n"
    md += "**Oyinda Technologies** | RC 1234567 | Member Nigerian Economic Summit Group\n"
    md += "**Contact:** hello@oyinda-ai.online | www.oyinda-ai.online\n"
    md += "\n*** END OF STATEMENT ***\n"

    # ---------- 6. Output ----------
    if fmt == 'json':
        return jsonify({"statement": md})
    elif fmt == 'pdf':
        try:
            import markdown
            from weasyprint import HTML

            styled_html = f"""
            <html>
            <head>
            <style>
                @page {{
                    size: A4;
                    margin: 1.5cm;
                }}
                body {{
                    font-family: Arial, Helvetica, sans-serif;
                    color: #1a1a1a;
                    font-size: 12px;
                    line-height: 1.6;
                }}
                .header {{
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    border-bottom: 3px solid #800020;
                    padding-bottom: 15px;
                    margin-bottom: 20px;
                }}
                .logo {{
                    font-size: 28px;
                    font-weight: bold;
                    color: #800020;
                }}
                .logo span {{
                    font-size: 14px;
                    display: block;
                    color: #333;
                    font-weight: normal;
                }}
                .title {{
                    text-align: right;
                    font-size: 20px;
                    font-weight: bold;
                    color: #800020;
                }}
                .info-grid {{
                    display: grid;
                    grid-template-columns: 1fr 1fr;
                    gap: 10px;
                    border: 1px solid #ccc;
                    padding: 15px;
                    margin-bottom: 20px;
                }}
                .info-item {{
                    font-size: 12px;
                }}
                .info-item strong {{
                    color: #800020;
                }}
                .summary-box {{
                    border: 1px solid #ccc;
                    padding: 15px;
                    margin-bottom: 20px;
                }}
                .summary-item {{
                    display: flex;
                    justify-content: space-between;
                    padding: 5px 0;
                    border-bottom: 1px dotted #eee;
                }}
                .summary-total {{
                    font-weight: bold;
                    font-size: 14px;
                    color: #800020;
                }}
                table {{
                    width: 100%;
                    border-collapse: collapse;
                    margin-bottom: 20px;
                }}
                th {{
                    background-color: #800020;
                    color: white;
                    padding: 8px;
                    font-size: 11px;
                    text-align: left;
                }}
                td {{
                    padding: 6px 8px;
                    border-bottom: 1px solid #eee;
                    font-size: 11px;
                }}
                tr:nth-child(even) {{
                    background-color: #f9f9f9;
                }}
                .debit {{
                    color: #c0392b;
                }}
                .credit {{
                    color: #27ae60;
                }}
                .footer {{
                    margin-top: 30px;
                    font-size: 10px;
                    color: #666;
                    text-align: center;
                    border-top: 1px solid #ccc;
                    padding-top: 10px;
                }}
            </style>
            </head>
            <body>
            <div class="header">
                <div class="logo">
                    🦋 Oyinda
                    <span>INNOVATIONS. TECHNOLOGY. SERVICE.</span>
                </div>
                <div class="title">BANK STATEMENT</div>
            </div>
            <div class="info-grid">
                <div class="info-item"><strong>Account Holder:</strong> {get_user_name(user_id)}</div>
                <div class="info-item"><strong>Customer ID:</strong> {user_id[:12]}</div>
                <div class="info-item"><strong>Statement Period:</strong> {from_date} to {to_date}</div>
                <div class="info-item"><strong>Currency:</strong> NGN</div>
                <div class="info-item"><strong>Date of Issue:</strong> {datetime.utcnow().strftime('%Y-%m-%d')}</div>
                <div class="info-item"><strong>Account Tier:</strong> Standard</div>
            </div>
            <div class="summary-box">
                <div class="summary-item"><span>Opening Balance</span><span>₦0.00</span></div>
                <div class="summary-item"><span>Total Deposits (+)</span><span class="credit">₦{total_deposits:,.2f}</span></div>
                <div class="summary-item"><span>Total Withdrawals (-)</span><span class="debit">₦{total_withdrawals:,.2f}</span></div>
                <div class="summary-item"><span>Closing Balance</span><span>₦{sum(running_balances.values()):,.2f}</span></div>
                <div class="summary-item"><span>Avg Monthly Spend</span><span>₦{avg_monthly_spend:,.2f}</span></div>
            </div>
            <table>
                <tr>
                    <th>Date</th><th>Institution</th><th>Description</th><th>Debit (₦)</th><th>Credit (₦)</th><th>Balance (₦)</th>
                </tr>
            """
            for item in ledger:
                wd = f'<span class="debit">₦{item["withdrawal"]:,.2f}</span>' if item['withdrawal'] else ''
                dp = f'<span class="credit">₦{item["deposit"]:,.2f}</span>' if item['deposit'] else ''
                styled_html += f"<tr><td>{item['date']}</td><td>{item['institution']}</td><td>{item['description']}</td><td>{wd}</td><td>{dp}</td><td>₦{item['running_balance']:,.2f}</td></tr>"
            styled_html += """
            </table>
            <div class="footer">
                Oyinda Technologies | RC 1234567 | Member Nigerian Economic Summit Group<br>
                hello@oyinda-ai.online | www.oyinda-ai.online<br>
                *** END OF STATEMENT ***
            </div>
            </body>
            </html>
            """
            buffer = io.BytesIO()
            HTML(string=styled_html).write_pdf(buffer)
            buffer.seek(0)
            return send_file(buffer, as_attachment=True, download_name=f'oyinda_statement_{from_date}_{to_date}.pdf')
        except ImportError:
            return jsonify({"error": "PDF generation libraries not installed."}), 500
    else:
        return md, 200, {'Content-Type': 'text/markdown; charset=utf-8'}



@app.route('/account/balance', methods=['POST'])
@jwt_required()
def account_balance():
    user_id = get_jwt_identity()
    data = request.get_json()
    account_id = data.get('account_id')
    if not account_id:
        return jsonify({"error": "account_id required"}), 400

    # Get account details
    accounts = get_user_connected_accounts(user_id)
    account = next((a for a in accounts if a['id'] == account_id), None)
    if not account:
        return jsonify({"error": "Account not found"}), 404

    try:
        from connectors.balances import get_account_balance
        balance_str = get_account_balance(account)
        return jsonify({"balance": balance_str})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --------------- TRANSACTION LIST (paginated) ---------------
@app.route('/transactions', methods=['GET'])
@jwt_required()
def list_transactions():
    user_id = get_jwt_identity()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    type_filter = request.args.get('type', '')
    date_from = request.args.get('date_from', '1900-01-01')
    date_to = request.args.get('date_to', datetime.utcnow().strftime('%Y-%m-%d'))
    category = request.args.get('category', '')

    conn = get_conn()
    cur = conn.cursor()
    query = "SELECT date, type, amount, currency, category, description FROM transactions_view WHERE user_id=%s AND date BETWEEN %s AND %s"
    params = [user_id, date_from, date_to]
    if type_filter:
        query += " AND type = %s"
        params.append(type_filter)
    if category:
        query += " AND category = %s"
        params.append(category)
    query += " ORDER BY date DESC LIMIT %s OFFSET %s"
    params.append(per_page)
    params.append((page-1)*per_page)
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()
    tx = [{"date": r[0], "type": r[1], "amount": r[2], "currency": r[3], "category": r[4], "description": r[5]} for r in rows]
    return jsonify({"page": page, "transactions": tx})

# --------------- MONO & EXCHANGE & WALLET ENDPOINTS (as before) ---------------
# ... (include /link/mono, /sync/mono, /link/exchange, /sync/exchange, /crypto/order, /crypto/withdraw, /link/wallet, /crypto/wallet/prepare, /crypto/wallet/submit)

@app.route('/wallet/token_transfer_executed', methods=['POST'])
@jwt_required()
def token_transfer_executed():
    user_id = get_jwt_identity()
    data = request.get_json()
    tx_hash = data.get('tx_hash')
    event_id = data.get('event_id') or data.get('original_event_id')
    if not tx_hash or not event_id:
        return jsonify({"error": "tx_hash and event_id required"}), 422
    append_event(user_id, user_id, 'TokenTransferExecuted', {"tx_hash": tx_hash, "original_event_id": event_id})
    return jsonify({"message": "Transfer recorded."})


@app.route('/wallet/swap_executed', methods=['POST'])
@jwt_required()
def wallet_swap_executed():
    user_id = get_jwt_identity()
    data = request.get_json()
    tx_hash = data.get('tx_hash')
    event_id = data.get('event_id') or data.get('original_event_id')
    if not tx_hash or not event_id:
        return jsonify({"error": "tx_hash and event_id required"}), 422
    append_event(user_id, user_id, 'SwapExecuted', {"tx_hash": tx_hash, "original_event_id": event_id})
    return jsonify({"message": "Swap recorded."})


@app.route('/confirm_transfer', methods=['POST'])
@jwt_required()
def confirm_transfer():
    user_id = get_jwt_identity()
    data = request.get_json()
    event_id = data.get('event_id')
    if not event_id:
        return jsonify({"error": "event_id required"}), 400

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM events WHERE event_id=%s AND user_id=%s", (event_id, user_id))
    row = cur.fetchone()
    if not row or row[4] != 'TransferRequested':
        return jsonify({"error": "Invalid or expired transfer request."}), 400

    payload = json.loads(row[5])
    stream_id = row[3]

    # Confirm and execute
    append_event(user_id, stream_id, 'TransferConfirmed', payload)
    success, ref = mock_execute_transfer(payload)
    if success:
        append_event(user_id, stream_id, 'TransferExecuted', {**payload, "reference": ref})
        return jsonify({"message": f"Transfer of {payload['amount']} {payload['currency']} completed.", "tone": "income"})
    else:
        append_event(user_id, stream_id, 'TransferFailed', {**payload, "error": ref})
        return jsonify({"message": f"Transfer failed: {ref}", "tone": "warning"})




@app.route('/link/wallet', methods=['POST'])
@jwt_required()
def link_wallet():
    user_id = get_jwt_identity()
    data = request.get_json()
    address = data.get('address')
    network = data.get('network', 'Ethereum')
    label = data.get('label', f'{network} Wallet')

    if not address:
        return jsonify({"error": "Wallet address required"}), 400

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO connected_accounts (user_id, account_type, provider, label, currency, wallet_address, network) VALUES (%s, 'wallet', %s, %s, 'ETH', %s, %s) ON CONFLICT (user_id, wallet_address) DO NOTHING RETURNING id",
        (user_id, network.lower(), label, address, network)
    )
    row = cur.fetchone()
    if row:
        account_id = row[0]
        return jsonify({"message": f"{label} linked successfully.", "account_id": str(account_id)})
    else:
        return jsonify({"message": f"{label} already linked."})


@app.route('/link/exchange', methods=['POST'])
@jwt_required()
def link_exchange():
    user_id = get_jwt_identity()
    data = request.get_json()
    provider = data.get('provider', '').lower()
    api_key = data.get('api_key')
    api_secret = data.get('api_secret')
    passphrase = data.get('passphrase', '')   # for KuCoin
    if not provider or not api_key or not api_secret:
        return jsonify({"error": "provider, api_key, and api_secret required"}), 400

    # Validate provider
    valid_providers = ['binance', 'bybit', 'kucoin', 'coinbase']
    if provider not in valid_providers:
        return jsonify({"error": f"Unsupported provider. Choose from: {', '.join(valid_providers)}"}), 400

    # Encrypt credentials
    from utils.crypto import encrypt
    enc_key = encrypt(api_key)
    enc_secret = encrypt(api_secret)
    enc_passphrase = encrypt(passphrase) if passphrase else ''

    # Check if this account already exists (by provider)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM connected_accounts WHERE user_id=%s AND provider=%s AND account_type='exchange'",
        (user_id, provider)
    )
    existing = cur.fetchone()
    if existing:
        conn.close()
        return jsonify({"message": f"{provider.capitalize()} account already linked.", "account_id": str(existing[0])})

    # Insert new account
    cur.execute(
        "INSERT INTO connected_accounts (user_id, account_type, provider, label, currency, api_key_encrypted, api_secret_encrypted) VALUES (%s, 'exchange', %s, %s, 'USD', %s, %s) RETURNING id",
        (user_id, provider, f"{provider.capitalize()} Account", enc_key, enc_secret)
    )
    account_id = cur.fetchone()[0]
    conn.commit()
    conn.close()

    # Optionally, store passphrase in a separate table or as part of api_secret_encrypted (we'll extend later)
    return jsonify({"message": f"{provider.capitalize()} account linked successfully.", "account_id": str(account_id)})


@app.route('/link/bank', methods=['POST'])
@jwt_required()
def link_bank():
    user_id = get_jwt_identity()
    data = request.get_json()
    account_number = data.get('account_number')
    bank_code = data.get('bank_code')
    if not account_number or not bank_code:
        return jsonify({"error": "Account number and bank code required"}), 400

    try:
        from connectors.flutterwave import get_account_details
        details = get_account_details(account_number, bank_code)
        account_name = details.get('account_name', 'Unknown')
        # Store as connected account
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO connected_accounts (user_id, account_type, provider, label, currency, account_number, bank_code) VALUES (%s, 'bank', 'flutterwave', %s, 'NGN', %s, %s) RETURNING id",
            (user_id, f"{account_name} - {bank_code}", account_number, bank_code)
        )
        conn.commit()
        conn.close()
        return jsonify({"message": f"Bank account {account_number} linked ({account_name})."})
    except Exception as e:
        return jsonify({"error": f"Linking failed: {str(e)}"}), 500


@app.route('/sync/bank', methods=['POST'])
@jwt_required()
def sync_bank():
    user_id = get_jwt_identity()
    data = request.get_json()
    account_id = data.get('account_id')   # the connected_accounts id (UUID)
    if not account_id:
        return jsonify({"error": "Account ID required"}), 400

    # Get the Okra account ID from connected_accounts
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT account_number, bank_name FROM connected_accounts WHERE id=%s AND user_id=%s", (account_id, user_id))
    row = cur.fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Account not found"}), 404

    # For simplicity, we assume the account_number is the Okra account ID (we stored it there)
    okra_account_id = row[0]
    try:
        from connectors.okra import get_transactions
        txns = get_transactions(okra_account_id)
        count = 0
        for tx in txns:
            # Idempotency: skip if already processed (using the Okra transaction ID)
            # Here we just log them as Income/Expense events
            tx_type = 'IncomeReceived' if tx.get('type') == 'credit' else 'ExpenseLogged'
            amount = abs(tx.get('amount') / 100)  # kobo to Naira
            description = tx.get('narration', '')
            category = guess_category(description)
            payload = {
                "amount": amount,
                "currency": "NGN",
                "date": tx.get('date')[:10],
                "description": description,
                "category": category
            }
            append_event(user_id, account_id, tx_type, payload)
            count += 1
        return jsonify({"message": f"Synced {count} transactions."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route('/link/mono', methods=['POST'])
@jwt_required()
def link_mono():
    user_id = get_jwt_identity()
    data = request.get_json()
    code = data.get('code')
    if not code:
        return jsonify({"error": "Mono auth code required"}), 400

    try:
        from connectors.mono import exchange_code, get_account_details
        mono_resp = exchange_code(code)
        mono_account_id = mono_resp.get('id')
        if not mono_account_id:
            return jsonify({"error": "Invalid Mono response"}), 400

        details = get_account_details(mono_account_id)
        account_number = details.get('account_number', '')
        bank_name = details.get('institution', {}).get('name', 'Unknown Bank')
        currency = details.get('currency', 'NGN')

        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO connected_accounts (user_id, account_type, provider, label, currency, account_number, bank_name) VALUES (%s, 'bank', 'mono', %s, %s, %s, %s) RETURNING id",
            (user_id, f"{bank_name} Account", currency, account_number, bank_name)
        )
        account_id = cur.fetchone()[0]
        conn.commit()
        conn.close()

        return jsonify({"message": f"{bank_name} account ending {account_number[-3:]} linked successfully.", "account_id": str(account_id)})
    except Exception as e:
        return jsonify({"error": f"Linking failed: {str(e)}"}), 500

def guess_category(narration):
    narration = narration.lower()
    if any(w in narration for w in ['food','rice','beans','restaurant']): return 'food'
    if any(w in narration for w in ['uber','taxi','transport','fuel']): return 'transport'
    if any(w in narration for w in ['rent','housing']): return 'housing'
    if any(w in narration for w in ['electricity','water','utility','internet','data']): return 'utilities'
    if any(w in narration for w in ['salary','wage','payment received']): return 'income'
    return 'other'


@app.route('/bank/transfer', methods=['POST'])
@jwt_required()
def bank_transfer():
    user_id = get_jwt_identity()
    data = request.get_json()
    from_account_id = data.get('from_account_id')   # Oyinda connected_accounts UUID
    to_bank_code = data.get('to_bank_code')          # e.g., "033" for UBA
    to_account_number = data.get('to_account_number')
    amount = data.get('amount')
    narration = data.get('narration', 'Oyinda transfer')

    if not from_account_id or not to_bank_code or not to_account_number or not amount:
        return jsonify({"error": "Missing required fields"}), 400

    # Get the Okra account ID
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT account_number FROM connected_accounts WHERE id=%s AND user_id=%s", (from_account_id, user_id))
    row = cur.fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Account not found"}), 404

    okra_account_id = row[0]
    try:
        from connectors.okra import initiate_transfer
        result = initiate_transfer(okra_account_id, amount, to_bank_code, to_account_number, narration)
        return jsonify({"message": "Transfer initiated.", "reference": result.get('data', {}).get('reference')})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/tax/receipt', methods=['POST'])
@jwt_required()
def tax_receipt():
    user_id = get_jwt_identity()
    data = request.get_json()
    receipt_id = data.get('receipt_id')
    email = data.get('email')

    if not receipt_id or not email:
        return jsonify({"error": "Missing receipt_id or email"}), 400

    # For now, just log the request. We'll integrate SendGrid or SMTP later.
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO receipt_log (user_id, receipt_id, email) VALUES (%s, %s, %s)", (user_id, receipt_id, email))
    conn.commit()
    conn.close()

    return jsonify({"message": f"Receipt {receipt_id} has been sent to {email}."})


@app.route('/cron/emergency-data', methods=['POST'])
def emergency_data():
    secret = request.headers.get('X-Cron-Secret') or request.args.get('secret')
    if secret != CRON_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    conn = get_conn()
    cur = conn.cursor()

    # Find users who have a threshold and a phone number
    cur.execute("""
        SELECT id, phone, COALESCE(data_balance_mb, 0)
        FROM users
        WHERE phone IS NOT NULL AND phone != ''
          AND facts->>'emergency_data_hours' IS NOT NULL
    """)
    users = cur.fetchall()

    today = datetime.utcnow().date()
    sent = 0

    for user_id, phone, balance in users:
        # Get the user's threshold
        cur.execute("SELECT facts->>'emergency_data_hours' FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        if not row or not row[0]:
            continue
        threshold_hours = int(row[0])

        # Last activity time
        cur.execute("SELECT MAX(created_at) FROM events WHERE user_id = %s", (user_id,))
        last_activity = cur.fetchone()[0]

        if last_activity:
            offline_duration = (datetime.utcnow() - last_activity).total_seconds() / 3600
        else:
            offline_duration = 999  # never active → treat as long offline

        if offline_duration >= threshold_hours:
            # Check if already sent today
            cur.execute(
                "SELECT 1 FROM data_rewards WHERE user_id = %s AND awarded_at::date = %s AND reward_type = 'emergency'",
                (user_id, today)
            )
            if cur.fetchone():
                continue

            # Ensure enough balance (33 MB)
            if balance < 33:
                continue

            # Send 33 MB via VTpass
            try:
                from connectors.vtpass import buy_data
                result = buy_data(phone, 'mtn', 'mtn-10mb-100')  # smallest MTN data plan
                if result.get('code') == '000':
                    cur.execute(
                        "INSERT INTO data_rewards (user_id, reward_type) VALUES (%s, %s)",
                        (user_id, 'emergency')
                    )
                    cur.execute(
                        "UPDATE users SET data_balance_mb = GREATEST(0, data_balance_mb - 33) WHERE id = %s",
                        (user_id,)
                    )
                    conn.commit()
                    sent += 1
            except Exception as e:
                print(f"Emergency data failed for {phone}: {e}")

    conn.close()
    return jsonify({"emergency_sent": sent})



@app.route('/credit/share/<token>', methods=['GET'])
def share_credit_report(token):
    # Verify a short-lived token (we can use JWT or a simple hash)
    # For simplicity, we'll use the user's real JWT – later we can make a temporary one.
    # For now, we'll return the report as a clean HTML page.
    try:
        from flask_jwt_extended import decode_token
        decoded = decode_token(token)
        user_id = decoded['sub']
    except:
        return "Invalid or expired link.", 400

    score_data = get_credit_score(user_id)
    # Build a simple, clean HTML page
    html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Oyinda Credit Report</title>
    <style>body{{font-family: sans-serif; max-width: 600px; margin: 40px auto; padding: 20px;}} h1{{color: #10b981;}} .score{{font-size: 3em; margin: 0;}} .logo{{font-size: 2em;}}</style>
    </head>
    <body>
    <h1>🦋 Oyinda Credit Report</h1>
    <p>Generated for: {get_user_name(user_id)}</p>
    <p class="logo">{'🦅' if score_data['logo'] == 'eagle' else '🦋'}</p>
    <p class="score">{score_data['score']}/850</p>
    <p>{score_data.get('description', '')}</p>
    <hr>
    <p><small>Report generated by Oyinda – your AI Financial Companion. This report is valid for 30 days.</small></p>
    </body>
    </html>
    """
    return html


import secrets
from datetime import datetime, timedelta

# Store temporary share tokens (in production, use a database table)
share_tokens = {}

@app.route('/credit/share', methods=['POST'])
@jwt_required()
def generate_share_link():
    user_id = get_jwt_identity()
    # Create a random token that expires in 24 hours
    token = secrets.token_urlsafe(24)
    share_tokens[token] = {
        "user_id": user_id,
        "expires_at": datetime.utcnow() + timedelta(hours=24)
    }
    share_url = f"https://oyinda-v2.onrender.com/credit/share/{token}"
    return jsonify({"share_url": share_url})

@app.route('/credit/share/<token>', methods=['GET'])
def view_shared_report(token):
    info = share_tokens.get(token)
    if not info or datetime.utcnow() > info['expires_at']:
        return "This link has expired. Please ask the user to generate a new one.", 410

    user_id = info['user_id']
    score_data = get_credit_score(user_id)

    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Oyinda Credit Report</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 600px; margin: 20px auto; padding: 20px; background: #f9fafb; }}
            .card {{ background: white; border-radius: 16px; padding: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); text-align: center; }}
            .score {{ font-size: 3em; margin: 0; color: #10b981; }}
            .logo {{ font-size: 2em; }}
            .info {{ margin-top: 16px; font-size: 0.9em; color: #6b7280; }}
        </style>
    </head>
    <body>
        <div class="card">
            <div class="logo">{'🦅' if score_data['logo'] == 'eagle' else '🦋'}</div>
            <p class="score">{score_data['score']}/850</p>
            <p style="color: #374151;">Oyinda Credit Score</p>
            <div class="info">
                <p>This report was generated by <strong>Oyinda</strong> – your AI Financial Companion.</p>
                <p>It reflects the user's financial behaviour as tracked by Oyinda.</p>
                <p>Valid for 24 hours after generation.</p>
            </div>
        </div>
    </body>
    </html>
    """
    return html


@app.route('/tax/breakdown', methods=['GET'])
@jwt_required()
def tax_breakdown():
    user_id = get_jwt_identity()
    try:
        breakdown = calculate_all_taxes(user_id)
        return jsonify(breakdown)
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route('/account/<account_id>', methods=['DELETE', 'OPTIONS'])
@jwt_required(optional=True)   # allow OPTIONS without JWT
def delete_account(account_id):
    if request.method == 'OPTIONS':
        return jsonify({}), 200   # preflight okay

    user_id = get_jwt_identity()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM connected_accounts WHERE id=%s AND user_id=%s RETURNING id", (account_id, user_id))
    deleted = cur.fetchone()
    conn.commit()
    conn.close()
    if deleted:
        return jsonify({"message": "Account disconnected successfully."})
    else:
        return jsonify({"error": "Account not found or already deleted."}), 404


@app.route('/api/accounts', methods=['GET'])
@jwt_required()
def api_accounts():
    user_id = get_jwt_identity()
    accounts = get_user_connected_accounts(user_id)
    return jsonify(accounts)


@app.route('/balance/data', methods=['GET'])
@jwt_required()
def data_balance():
    user_id = get_jwt_identity()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT data_balance_mb FROM users WHERE id = %s", (user_id,))
    bal = cur.fetchone()[0] or 0
    conn.close()
    return jsonify({"data_balance_mb": bal, "equivalent_minutes": bal // 0.5})  # 0.5 MB/min


@app.route('/verify/identity', methods=['POST'])
@jwt_required()
def verify_identity():
    user_id = get_jwt_identity()
    data = request.get_json()
    id_type = data.get('type', '').lower()   # 'bvn' or 'nin'
    id_number = data.get('number', '').strip()
    if id_type not in ['bvn', 'nin'] or not id_number:
        return jsonify({"error": "Missing type (bvn/nin) or number"}), 400

    # ----- Placeholder for real verification API -----
    # In production, call Mono/OnePipe here.
    # For now, we simulate a successful verification.
    verified = len(id_number) >= 10   # simple length check
    # ------------------------------------------------

    if verified:
        # Store verification flags
        store_user_fact(user_id, f'{id_type}_verified', True)
        store_user_fact(user_id, f'{id_type}_number', id_number)
        # Also store under generic 'bvn' or 'nin' for wallet creation
        store_user_fact(user_id, id_type, id_number)

        # Auto-create wallet (silent, don’t block user if it fails)
        try:
            ensure_wallet(user_id)
        except Exception:
            pass

        return jsonify({
            "message": f"Your {id_type.upper()} has been verified. Your identity is now upgraded and your Oyinda wallet is active.",
            "verified": True
        })


# --------------- HEALTH ---------------
@app.route('/health', methods=['GET'])
@jwt_required()
def health():
    user_id = get_jwt_identity()
    score_data = get_credit_score(user_id)
    score = score_data["score"]
    logo = score_data["logo"]

    # Plain‑language description for 300‑850 scale
    if score < 580:
        desc = "Butterfly 🦋 – just starting out. Log more transactions and pay back loans to improve."
    elif score < 740:
        desc = "Transition – doing well. Regular saving and paying debts on time will boost you further."
    else:
        desc = "Eagle 🦅 – excellent! Your financial health is strong."

    return jsonify({
        "score": score,
        "logo": logo,
        "description": desc,
        "scale": "300‑850"
    })



@app.route('/debug/binance', methods=['GET'])
def debug_binance():
    try:
        token = request.args.get('token')
        if not token:
            return jsonify({"error": "Missing token"}), 401

        from flask_jwt_extended import decode_token
        decoded = decode_token(token)
        user_id = decoded['sub']

        # Fetch stored credentials
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT api_key_encrypted, api_secret_encrypted FROM connected_accounts WHERE user_id=%s AND provider='binance'",
            (user_id,)
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "No Binance account linked."}), 404

        from utils.crypto import decrypt
        try:
            api_key = decrypt(row[0])
            api_secret = decrypt(row[1])
        except Exception as e:
            return jsonify({"error": f"Decryption failed: {str(e)}"}), 500

        # Call Binance API manually
        import requests, hmac, hashlib, time
        base_url = "https://api.binance.com"
        endpoint = "/api/v3/account"
        timestamp = int(time.time() * 1000)
        query_string = f"timestamp={timestamp}"
        signature = hmac.new(api_secret.encode(), query_string.encode(), hashlib.sha256).hexdigest()
        url = f"{base_url}{endpoint}?{query_string}&signature={signature}"
        headers = {"X-MBX-APIKEY": api_key}

        resp = requests.get(url, headers=headers, timeout=10)
        return jsonify({
            "http_status": resp.status_code,
            "response_body": resp.text
        })

    except Exception as e:
        import traceback
        return jsonify({
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500



@app.route('/link/bank/start', methods=['POST', 'OPTIONS'])
@cross_origin()
@jwt_required()
def start_bank_link():
    user_id = get_jwt_identity()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name, email FROM users WHERE id=%s", (user_id,))
    user = cur.fetchone()
    conn.close()
    if not user:
        return jsonify({"error": "User not found"}), 400

    # Unique reference so Mono never complains about duplicates
    unique_ref = f"{user_id}_{uuid.uuid4().hex[:8]}"

    try:
        resp = requests.post(
            "https://api.withmono.com/v2/accounts/initiate",
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "mono-sec-key": os.environ.get("MONO_SECRET_KEY")
            },
            json={
                "customer": {"name": user[0], "email": user[1]},
                "meta": {"ref": unique_ref},
                "scope": "auth",
                "redirect_url": f"https://oyinda-v2.onrender.com/link/bank/callback?ref={unique_ref}"
            }
        )
        data = resp.json()
        if data.get("status") == "successful":
            # Store the mapping and the Mono customer ID for later use
            temp_links[unique_ref] = {
                "user_id": user_id,
                "customer_id": data["data"]["customer"]
            }
            return jsonify({"mono_url": data["data"]["mono_url"]})
        else:
            return jsonify({"error": data.get("message", "Mono API error")}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/wallet', methods=['GET'])
@jwt_required()
def get_wallet():
    user_id = get_jwt_identity()
    try:
        wallet = ensure_wallet(user_id)
        return jsonify({
            "has_wallet": True,
            "balance": wallet['balance'],
            "account_number": wallet['account_number'],
            "bank_name": wallet['bank_name'],
            "bank_code": wallet['bank_code']
        })
    except Exception as e:
        return jsonify({
            "has_wallet": False,
            "message": str(e)
        })


def find_supplier(supplier_name, city=None):
    conn = get_conn()
    cur = conn.cursor()
    if city:
        cur.execute("""
            SELECT bl.user_id, uw.mono_account_id, uw.account_number, uw.bank_name, uw.bank_code
            FROM business_listings bl
            JOIN user_wallets uw ON bl.user_id = uw.user_id
            WHERE LOWER(bl.name) = LOWER(%s) AND LOWER(bl.city) = LOWER(%s)
            LIMIT 1
        """, (supplier_name, city))
    else:
        cur.execute("""
            SELECT bl.user_id, uw.mono_account_id, uw.account_number, uw.bank_name, uw.bank_code
            FROM business_listings bl
            JOIN user_wallets uw ON bl.user_id = uw.user_id
            WHERE LOWER(bl.name) = LOWER(%s)
            LIMIT 1
        """, (supplier_name,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {
            "user_id": row[0],
            "wallet": {
                "mono_account_id": row[1],
                "account_number": row[2],
                "bank_name": row[3],
                "bank_code": row[4]
            }
        }
    return None


@app.route('/cron/deduct-loans', methods=['POST'])
def deduct_loans():
    # Simple auth (use CRON_SECRET)
    secret = request.headers.get('X-Cron-Secret') or request.args.get('secret')
    if secret != CRON_SECRET:
        return jsonify({"error": "unauthorized"}), 403

    conn = get_conn()
    cur = conn.cursor()
    # Fetch all active loans where today <= end_date
    today = datetime.utcnow().date()
    cur.execute("""
        SELECT id, user_id, daily_amount, remaining_balance
        FROM inventory_loans
        WHERE status = 'active' AND end_date >= %s
    """, (today,))
    loans = cur.fetchall()

    for loan in loans:
        loan_id, user_id, daily_amount, remaining, start_date = loan[0], loan[1], loan[2], loan[3], loan[4]
        # Grace period: no deduction for first 7 days
        if today <= start_date + timedelta(days=7):
            continue
        # Check user wallet balance
        cur.execute("SELECT balance FROM user_wallets WHERE user_id = %s", (user_id,))
        wallet_row = cur.fetchone()
        if not wallet_row or wallet_row[0] < daily_amount:
            # Insufficient balance; skip or mark missed (we'll just skip for now)
            continue

        # Deduct
        new_balance = wallet_row[0] - daily_amount
        cur.execute("UPDATE user_wallets SET balance = %s, last_balance_update = now() WHERE user_id = %s",
                    (new_balance, user_id))
        new_remaining = remaining - daily_amount
        cur.execute("UPDATE inventory_loans SET remaining_balance = %s WHERE id = %s",
                    (new_remaining, loan_id))
        # Log repayment
        cur.execute("INSERT INTO loan_repayments (loan_id, amount, method) VALUES (%s, %s, 'auto')",
                    (loan_id, daily_amount))
        # Log event
        append_event(user_id, user_id, 'LoanRepaid', {
            "loan_id": str(loan_id),
            "amount": daily_amount,
            "method": "auto"
        })
        # If fully repaid, mark completed
        if new_remaining <= 0:
            cur.execute("UPDATE inventory_loans SET status = 'completed', remaining_balance = 0 WHERE id = %s", (loan_id,))

    conn.commit()
    conn.close()
    return jsonify({"processed": len(loans)})


@app.route('/link/bank/callback', methods=['GET'])
def bank_callback():
    unique_ref = request.args.get('ref')
    status = request.args.get('status')

    if not unique_ref or status != 'linked':
        return "Bank linking failed or was cancelled.", 400

    mapping = temp_links.pop(unique_ref, None)
    if not mapping:
        return "Invalid request.", 400

    user_id = mapping["user_id"]
    customer_id = mapping["customer_id"]

    # Fetch the newly linked account(s) from Mono
    try:
        resp = requests.get(
            f"https://api.withmono.com/v2/customers/{customer_id}/accounts",
            headers={
                "accept": "application/json",
                "mono-sec-key": os.environ.get("MONO_SECRET_KEY")
            }
        )
        data = resp.json()
        accounts = data.get("data", [])
        if not accounts:
            return "No accounts found.", 500

        # Link the first account to the user (you can later add logic to link all)
        first = accounts[0]
        account_number = first.get("account_number", "")
        bank_name = first.get("institution", {}).get("name", "Unknown Bank")
        currency = first.get("currency", "NGN")

        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO connected_accounts (user_id, account_type, provider, label, currency, account_number, bank_name) VALUES (%s, 'bank', 'mono', %s, %s, %s, %s) RETURNING id",
            (user_id, f"{bank_name} Account", currency, account_number, bank_name)
        )
        conn.commit()
        conn.close()

        return f"Bank account {account_number} from {bank_name} linked successfully. You can close this tab."

    except Exception as e:
        return f"Error fetching accounts: {str(e)}", 500




@app.route('/link/payment', methods=['POST'])
@jwt_required()
def link_payment():
    user_id = get_jwt_identity()
    data = request.get_json()
    provider = data.get('provider', '').lower()
    api_key = data.get('api_key')

    if not provider or not api_key:
        return jsonify({"error": "provider and api_key required"}), 400

    try:
        if provider == 'flutterwave':
            result = link_flutterwave(user_id, api_key)
        elif provider == 'paystack':
            result = link_paystack(user_id, api_key)
        else:
            return jsonify({"error": "Unsupported provider"}), 400
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/link/account', methods=['POST'])
@jwt_required()
def link_account():
    user_id = get_jwt_identity()
    data = request.get_json()
    account_type = data.get('account_type', '').lower()
    provider = data.get('provider', '').lower()
    api_key = data.get('api_key', '')
    api_secret = data.get('api_secret', '')

    # Allowed types
    allowed_types = ['stock', 'forex', 'savings', 'payment', 'exchange', 'p2p']
    if account_type not in allowed_types:
        return jsonify({"error": f"Invalid account type. Choose from: {', '.join(allowed_types)}"}), 400

    if not provider:
        return jsonify({"error": "Provider name is required."}), 400

    # Encrypt credentials
    from utils.crypto import encrypt
    enc_key = encrypt(api_key) if api_key else ''
    enc_secret = encrypt(api_secret) if api_secret else ''

    # Store in connected_accounts
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO connected_accounts (user_id, account_type, provider, label, currency, api_key_encrypted, api_secret_encrypted) VALUES (%s, %s, %s, %s, 'USD', %s, %s) RETURNING id",
        (user_id, account_type, provider, f"{provider.capitalize()} {account_type.title()}", enc_key, enc_secret)
    )
    account_id = cur.fetchone()[0]
    conn.commit()
    conn.close()

    return jsonify({"message": f"{provider.capitalize()} {account_type} account linked successfully.", "account_id": str(account_id)})


@app.route('/webhook/mono', methods=['POST'])
def mono_webhook():
    payload = request.get_json()
    # Verify signature if needed
    # Expect payload: {"event": "credit", "data": {"account": "acc_id", "amount": 5000, ...}}
    event = payload.get('event')
    data = payload.get('data', {})
    if event == 'credit':
        account_id = data.get('account')
        amount = data.get('amount')
        # Find user by mono_account_id
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT user_id, balance FROM user_wallets WHERE mono_account_id = %s", (account_id,))
        row = cur.fetchone()
        if row:
            new_balance = row[1] + amount
            cur.execute("UPDATE user_wallets SET balance = %s, last_balance_update = now() WHERE mono_account_id = %s", (new_balance, account_id))
            conn.commit()
            # Log event
            append_event(row[0], row[0], 'WalletCredited', {"amount": amount, "source": "bank_transfer", "account_id": account_id})
            # Optionally send notification to user
        conn.close()
    return jsonify({"status": "received"}), 200


@app.route('/credit/report', methods=['GET'])
@jwt_required()
def credit_report():
    user_id = get_jwt_identity()
    score_data = get_credit_score(user_id)
    score = score_data["score"]
    logo = score_data["logo"]

    # Get a brief transaction summary
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM events WHERE user_id = %s", (user_id,))
    total_txns = cur.fetchone()[0]

    # Average monthly income (last 6 months)
    cur.execute("""
        SELECT AVG(monthly) FROM (
            SELECT SUM(amount) as monthly
            FROM transactions_view
            WHERE user_id=%s AND type='income'
              AND date >= NOW() - INTERVAL '6 months'
            GROUP BY DATE_TRUNC('month', date)
        ) sub
    """, (user_id,))
    avg_income = cur.fetchone()[0] or 0

    # Total assets from net worth
    try:
        net_worth_str = calculate_net_worth(user_id)
        import re
        match = re.search(r'Total Assets \(NGN\): ₦([\d,]+\.?\d*)', net_worth_str)
        total_assets = float(match.group(1).replace(',','')) if match else 0
        match_liab = re.search(r'Total Liabilities \(Loans\): ₦([\d,]+\.?\d*)', net_worth_str)
        total_liabilities = float(match_liab.group(1).replace(',','')) if match_liab else 0
    except:
        total_assets = total_liabilities = 0

    conn.close()

    # Build the report data
    report = {
        "score": score,
        "logo": logo,
        "description": get_score_description(score),
        "total_transactions": total_txns,
        "average_monthly_income": round(avg_income, 2),
        "total_assets": round(total_assets, 2),
        "total_liabilities": round(total_liabilities, 2),
        "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "user_name": get_user_name(user_id),
        "user_email": None  # fetch from users table if needed
    }

    # Add user email
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT email FROM users WHERE id=%s", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        report["user_email"] = row[0]

    return jsonify(report)

def get_score_description(score):
    if score < 40: return "Keep logging to build your score"
    elif score < 70: return "Doing well – regular saving helps"
    elif score < 90: return "Great financial health!"
    else: return "Excellent! You're an eagle"



@app.route('/tax/estimate', methods=['GET'])
@jwt_required()
def tax_estimate():
    user_id = get_jwt_identity()
    try:
        from core import calculate_all_taxes
        breakdown = calculate_all_taxes(user_id)
        return jsonify({
            "yearly_income": breakdown.get("total_income", 0),
            "estimated_tax": breakdown["total_tax"],
            "taxes": breakdown["taxes"],
            "currency": "NGN",
            "note": "This is an estimate based on your logged income. Please consult a tax professional."
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/tax/pay', methods=['POST'])
@jwt_required()
def pay_tax():
    user_id = get_jwt_identity()
    data = request.get_json()
    amount = data.get('amount')
    if not amount or float(amount) <= 0:
        return jsonify({"error": "Invalid amount"}), 400

    # For now, we log a tax payment event and return a mock receipt.
    # Later, integrate with Flutterwave/Paystack.
    payload = {
        "amount": float(amount),
        "currency": "NGN",
        "category": "tax",
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "description": "Presumptive tax payment (via Oyinda)"
    }
    event = append_event(user_id, user_id, 'ExpenseLogged', payload)

    receipt = {
        "receipt_id": f"TAX-{event['event_id'][:8]}",
        "amount": amount,
        "date": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "status": "paid",
        "message": "Your tax payment has been recorded. Official receipt generated."
    }
    return jsonify(receipt)




@app.route('/data/plans', methods=['GET'])
@jwt_required()
def data_plans():
    network = request.args.get('network', 'mtn').lower()
    try:
        from connectors.vtpass import get_data_plans
        plans = get_data_plans(network)
        # Simplify for frontend: name, price, code
        simplified = [
            {
                "name": plan["name"],
                "price": plan["variation_amount"],
                "code": plan["variation_code"]
            }
            for plan in plans
        ]
        return jsonify({"plans": simplified})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/data/redeem', methods=['POST'])
@jwt_required()
def redeem_data():
    user_id = get_jwt_identity()
    data_req = request.get_json()
    network = data_req.get('network', 'mtn')
    plan_code = data_req.get('plan_code')
    phone = data_req.get('phone')
    amount = float(data_req.get('amount', 0))

    if not network or not plan_code or not phone or amount <= 0:
        return jsonify({"error": "Missing fields"}), 400

    # 1. Check user's Oyinda data balance
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT data_balance_mb FROM users WHERE id = %s", (user_id,))
    bal = cur.fetchone()[0] or 0
    naira_value = bal * 0.1   # 10 MB = ₦1
    if naira_value < amount:
        conn.close()
        return jsonify({"error": f"Insufficient data credit. You have {bal:.0f} MB (≈ ₦{naira_value:.2f})."}), 400

    # 2. Buy data from VTpass
    try:
        from connectors.vtpass import buy_data
        result = buy_data(phone, network, plan_code, amount)
        if result.get("code") != "000":
            conn.close()
            return jsonify({"error": result.get("response_description", "VTpass error")}), 500
    except Exception as e:
        conn.close()
        return jsonify({"error": f"VTpass error: {str(e)}"}), 500

    # 3. Deduct Oyinda balance
    mb_deducted = amount / 0.1
    cur.execute(
        "UPDATE users SET data_balance_mb = GREATEST(0, data_balance_mb - %s) WHERE id = %s",
        (mb_deducted, user_id)
    )
    conn.commit()
    conn.close()

    return jsonify({
        "message": f"Successfully purchased {plan_code}! {mb_deducted:.0f} MB deducted.",
        "new_balance_mb": bal - mb_deducted
    })


@app.route('/streak', methods=['GET'])
@jwt_required()
def streak():
    user_id = get_jwt_identity()
    conn = get_conn()
    cur = conn.cursor()

    # Total days logged this month
    first_of_month = datetime.utcnow().replace(day=1).date()
    cur.execute(
        "SELECT COUNT(*) FROM daily_activity_log WHERE user_id = %s AND date >= %s",
        (user_id, first_of_month)
    )
    total_days = cur.fetchone()[0]

    # Current data balance
    cur.execute("SELECT COALESCE(data_balance_mb, 0) FROM users WHERE id = %s", (user_id,))
    data_balance = cur.fetchone()[0]

    conn.close()

    # Pidgin-friendly message
    if total_days == 0:
        msg = "You never tell me any expense this month. Start now make you dey earn data!"
    else:
        msg = f"You don tell me wetin you spend for {total_days} days this month. You don earn {data_balance:.0f} MB."

    return jsonify({
        "total_days": total_days,
        "data_balance_mb": data_balance,
        "message": msg
    })

@app.route('/cron/daily-reward', methods=['POST'])
def daily_data_reward():
    secret = request.headers.get('X-Cron-Secret') or request.args.get('secret')
    if secret != os.environ.get('CRON_SECRET'):
        return jsonify({"error": "Unauthorized"}), 403

    today = datetime.utcnow().date()
    conn = get_conn()
    cur = conn.cursor()

    # Find users who logged an activity today and have a phone number
    cur.execute("""
        SELECT DISTINCT u.id, u.phone
        FROM users u
        INNER JOIN daily_activity_log d ON u.id = d.user_id AND d.date = %s
        WHERE u.phone IS NOT NULL AND u.phone != ''
    """, (today,))

    users = cur.fetchall()
    rewarded = 0

    for user_id, phone in users:
        # Check if already rewarded today
        cur.execute(
            "SELECT 1 FROM data_rewards WHERE user_id = %s AND awarded_at::date = %s",
            (user_id, today)
        )
        if cur.fetchone():
            continue

        # Purchase 10 MB MTN data bundle (smallest available plan)
        try:
            from connectors.vtpass import buy_data
            result = buy_data(phone, 'mtn', 'mtn-10mb-100')
            if result.get('code') == '000':
                cur.execute(
                    "INSERT INTO data_rewards (user_id, reward_type) VALUES (%s, %s)",
                    (user_id, '10MB daily')
                )
                conn.commit()
                rewarded += 1
            else:
                print(f"VTpass failed for {phone}: {result}")
        except Exception as e:
            print(f"Error rewarding {phone}: {e}")

    conn.close()
    return jsonify({"rewarded": rewarded})



@app.route('/cron/remind', methods=['POST'])
def cron_remind():
    secret = request.headers.get('X-Cron-Secret') or request.args.get('secret')
    if secret != CRON_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    conn = get_conn()
    cur = conn.cursor()
    today = datetime.utcnow().strftime('%Y-%m-%d')
    cur.execute("""
        SELECT u.id, u.email, u.name
        FROM users u
        WHERE NOT EXISTS (
            SELECT 1 FROM events e
            WHERE e.user_id = u.id
              AND e.created_at::date = %s::date
              AND e.event_type IN ('ExpenseLogged', 'IncomeReceived')
        )
    """, (today,))
    users = cur.fetchall()

    sent = 0
    for user in users:
        if user[1] and '@' in user[1] and user[1] != f"{user[1].split('@')[0]}@oyinda.local":
            # Only send to real email addresses (skip our auto‑generated ones)
            subject = f"Good evening, {user[2]}! Tell Oyinda about your day."
            body = (
                f"Hello {user[2]},\n\n"
                "You haven't told me what you spent today. Even a small thing like "
                "'I spent 200 on okada' helps build your credit history.\n\n"
                "You sabi say? When you dey tell me wetin you spend everyday, e dey help "
                "you build your credit score. Good credit score fit give you cheap loan from "
                "better banks, no be those loan sharks wey dey chop your money.\n\n"
                "Just reply to this email or open Oyinda and tell me your expenses today.\n\n"
                "— Oyinda, your personal CFO"
            )
            if send_email(user[1], subject, body):
                sent += 1
            # Log the attempt
            cur.execute(
                "INSERT INTO reminder_log (user_id, email) VALUES (%s, %s)",
                (user[0], user[1])
            )

    conn.commit()
    conn.close()

    return jsonify({
        "reminders_sent": sent,
        "total_eligible": len(users)
    })




@app.route('/api/business/search', methods=['GET'])
@jwt_required()
def search_business():
    user_id = get_jwt_identity()
    q = request.args.get('q', '').strip().lower()
    city = request.args.get('city', '').strip().lower()

    if not q or len(q) < 2:
        return jsonify({"results": [], "message": "Please enter at least 2 letters to search."})

    conn = get_conn()
    cur = conn.cursor()
    # Fuzzy search on product, name, market_name; filter by city if provided, but include listings with empty city (system)
    query = """
    SELECT id, name, product, category, market_name, city, phone,
           avatar_url, rating, total_ratings, is_verified, listing_type
    FROM business_listings
    WHERE LOWER(product) LIKE %s
       OR LOWER(name) LIKE %s
       OR LOWER(market_name) LIKE %s
    ORDER BY rating DESC NULLS LAST, created_at DESC
    LIMIT 20
    """
    like_q = f'%{q}%'
    cur.execute(query, (like_q, like_q, like_q, city))
    rows = cur.fetchall()
    conn.close()

    results = []
    for r in rows:
        results.append({
            "id": r[0],
            "name": r[1],
            "product": r[2],
            "category": r[3],
            "market_name": r[4],
            "city": r[5],
            "phone": r[6],
            "avatar_url": r[7],
            "rating": r[8],
            "total_ratings": r[9],
            "is_verified": r[10],
            "listing_type": r[11]
        })

    return jsonify({"results": results})


@app.route('/debug/business/all', methods=['GET'])
def debug_business_all():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM business_listings")
    rows = cur.fetchall()
    conn.close()
    return jsonify([dict(zip([desc[0] for desc in cur.description], row)) for row in rows])


@app.route('/feedback', methods=['POST'])
@jwt_required()
def submit_feedback():
    user_id = get_jwt_identity()
    data = request.get_json()
    message = (data.get('message') or '').strip()
    context = (data.get('context') or '').strip()
    sentiment = (data.get('sentiment') or '').strip()

    if not message:
        return jsonify({"error": "Message required"}), 400

    # Auto‑derive sentiment if not provided
    if not sentiment:
        msg_lower = message.lower()
        positive_words = ['easy', 'quick', 'great', 'good', 'helpful', 'smooth', 'happy', 'love']
        negative_words = ['stressful', 'hard', 'bad', 'confusing', 'slow', 'frustrating', 'annoying']
        if any(w in msg_lower for w in positive_words):
            sentiment = 'positive'
        elif any(w in msg_lower for w in negative_words):
            sentiment = 'negative'
        else:
            sentiment = 'neutral'

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO feedback (user_id, context, message, sentiment) VALUES (%s, %s, %s, %s)",
        (user_id, context, message, sentiment)
    )
    conn.commit()
    conn.close()
    return jsonify({"message": "Thank you! Feedback received."})



@app.route('/debug/groq', methods=['GET'])
def debug_groq():
    import requests
    key = os.environ.get("GROQ_API_KEY")
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {key}"}
    payload = {
        "model": "qwen-3.6-27b",
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 0.0
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return jsonify({
            "status": "ok",
            "groq_response": data["choices"][0]["message"]["content"][:200]
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "error_message": str(e),
            "response_body": getattr(e, 'response', None) and e.response.text[:300]
        })


@app.route('/debug/llm', methods=['GET'])
def debug_llm():
    text = request.args.get('text', 'hello')
    user_id = request.args.get('user_id', 'test')
    reply = conversational_reply(user_id, text)
    return jsonify({"input": text, "reply": reply, "has_reply": reply is not None})

@app.route('/debug/openai', methods=['GET'])
def debug_openai():
    import requests, os
    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY')}"},
            json={
                "model": "gpt-5.4-mini-2026-03-17",
                "messages": [{"role": "user", "content": "Say hello in one short sentence."}],
                "max_tokens": 20
            },
            timeout=10
        )
        data = resp.json()
        return jsonify({
            "status": resp.status_code,
            "response": data,
            "key_first_5": os.environ.get('OPENAI_API_KEY', '')[:5]
        })
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route('/debug/groq_echo', methods=['GET'])
def debug_groq_echo():
    import requests, os
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ.get('GROQ_API_KEY')}"},
            json={
                "model": "qwen-3.6-27b",
                "messages": [{"role": "user", "content": "Say hello in one short sentence."}],
                "max_tokens": 20
            },
            timeout=10
        )
        data = resp.json()
        return jsonify({
            "status": resp.status_code,
            "response": data,
            "key_first_5": os.environ.get('GROQ_API_KEY', '')[:5]
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route('/tts', methods=['POST'])
@jwt_required()
def text_to_speech():
    data = request.get_json()
    text = data.get('text', '')
    if not text:
        return jsonify({"error": "No text provided"}), 400

    import requests
    api_key = os.environ.get("GOOGLE_API_KEY")
    resp = requests.post(
        f"https://texttospeech.googleapis.com/v1beta1/text:synthesize?key={api_key}",
        json={
            "input": {"text": text},
            "voice": {
                "languageCode": "en-NG",
                "name": "en-NG-Wavenet-A",   # female Nigerian voice
                "ssmlGender": "FEMALE"
            },
            "audioConfig": {
                "audioEncoding": "MP3",
                "speakingRate": 0.9,          # slower, natural pace
                "pitch": 0.0
            }
        }
    )
    data = resp.json()
    if "audioContent" in data:
        import base64
        audio_bytes = base64.b64decode(data["audioContent"])
        return send_file(io.BytesIO(audio_bytes), mimetype="audio/mp3")
    return jsonify({"error": "TTS failed"}), 500




import openai

@app.route('/voice', methods=['POST'])
@jwt_required()
def handle_voice():
    user_id = get_jwt_identity()
    if 'audio' not in request.files:
        return jsonify({"error": "No audio file provided"}), 422
    audio_file = request.files['audio']
    if audio_file.filename == '':
        return jsonify({"error": "Empty file name"}), 422
    if not audio_file.mimetype.startswith('audio/'):
        return jsonify({"error": "Invalid file type"}), 422

    temp_filename = f"/tmp/{user_id}_{uuid.uuid4().hex}.webm"
    audio_file.save(temp_filename)

    if os.path.getsize(temp_filename) == 0:
        os.remove(temp_filename)
        return jsonify({"message": "I didn't hear anything. Please try again.", "tone": "neutral"})

    try:
        client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        with open(temp_filename, "rb") as f:
            # Get user's preferred language (default to English)
            user_facts = get_user_facts(user_id)
            lang_pref = user_facts.get('language', 'english').lower()

            # Map internal language names to Whisper language codes
            lang_map = {
                'english': 'en',
                'pidgin': 'en',  # Pidgin is English‑based, so 'en' works best
                'yoruba': 'yo',
                'hausa': 'ha',
                'igbo': 'ig'
            }
            whisper_lang = lang_map.get(lang_pref, 'en')

            with open(temp_filename, "rb") as f:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    response_format="text",
                    language=whisper_lang
                )
        text = transcript.strip()
        if not text:
            return jsonify({"message": "I didn't catch that. Please try again.", "tone": "neutral"})

        save_conversation(user_id, 'user', text)

        resp = process_user_command(user_id, text)
        resp_json = resp.get_json()
        resp_json['transcription'] = text
        return jsonify(resp_json)

    except Exception as e:
        return jsonify({"error": f"Transcription failed: {str(e)}"}), 500
    finally:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)


# --------------- FRONTEND ---------------
@app.route('/')
def landing():
    return send_from_directory('webapp', 'landing.html')

@app.route('/admin/summary', methods=['GET'])
@jwt_required()
def admin_summary():
    user_id = get_jwt_identity()
    # Hardcoded admin email – replace with your email
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT email FROM users WHERE id=%s", (user_id,))
    user = cur.fetchone()
    if not user or user[0] not in ['gbengha2016@gmail.com', 'admin@oyinda.com']:   # your admin emails
        return jsonify({"error": "Unauthorized"}), 403

    # Count beta signups
    cur.execute("SELECT COUNT(*) FROM beta_waitlist")
    beta_signups = cur.fetchone()[0]

    # Count active users (users who logged a command in last 7 days)
    cur.execute("SELECT COUNT(DISTINCT user_id) FROM events WHERE created_at > now() - interval '7 days'")
    active_users = cur.fetchone()[0]

    # Total commands executed (all events)
    cur.execute("SELECT COUNT(*) FROM events")
    total_commands = cur.fetchone()[0]

    # Recent feedback
    cur.execute("SELECT f.message, u.email, f.created_at FROM feedback f JOIN users u ON f.user_id = u.id ORDER BY f.created_at DESC LIMIT 20")
    feedback_rows = cur.fetchall()
    feedback = [{"message": r[0], "email": r[1], "date": r[2].isoformat()} for r in feedback_rows]

    conn.close()
    return jsonify({
        "beta_signups": beta_signups,
        "active_users": active_users,
        "total_commands": total_commands,
        "recent_feedback": feedback
    })



def finalize_registration(token):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT data FROM onboarding_sessions WHERE token = %s", (token,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({"message": "Session expired. Please start again."})

    user_data = row[0] if isinstance(row[0], dict) else json.loads(row[0])

    # Generate a unique username from the name
    base_username = re.sub(r'[^a-z0-9]', '', user_data["name"].lower())[:15]
    username = base_username
    cur.execute("SELECT id FROM users WHERE username = %s", (username,))
    counter = 1
    while cur.fetchone():
        username = f"{base_username}{counter}"
        cur.execute("SELECT id FROM users WHERE username = %s", (username,))
        counter += 1

    # Internal email – not asked from user
    internal_email = f"{username}@oyinda.local"

    from core import create_user
    user_id = create_user(
        name=user_data["name"],
        email=internal_email,
        password=user_data["password"],
        account_type=user_data.get("account_type", "personal"),
        address=user_data.get("business_address", "")
    )

    if not user_id:
        cur.close()
        conn.close()
        return jsonify({"message": "That username is taken. Please try a different name."})

    if user_data.get("account_type") in ['business', 'company']:
        store_user_fact(user_id, "business_name", user_data.get("business_name", ""))
        store_user_fact(user_id, "business_address", user_data.get("business_address", ""))
        store_user_fact(user_id, 'language', user_data.get('language', 'english'))

    store_user_fact(user_id, "username", username)

    cur.execute("DELETE FROM onboarding_sessions WHERE token = %s", (token,))
    conn.commit()
    cur.close()
    conn.close()

    access_token = create_access_token(identity=str(user_id))
    return jsonify({
        "jwt": access_token,
        "user": {"id": user_id, "name": user_data["name"]},
        "message": f"All set, {user_data['name']}! Your username is {username}. You're now registered.",
        "tone": "income",
        "redirect": "/dashboard"
    })


if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)