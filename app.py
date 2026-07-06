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
    "If the user asks to buy crypto, tell them to say exactly 'buy [amount] [coin] on [exchange]' and you will execute it immediately. "
    "If a user asks about their credit score, explain the five pillars (payment history, credit utilization, credit age, credit mix, new credit) and reference their actual numbers from the facts provided."

    "Oyinda features you can reference:\n"
    "- Credit score (0-100) with a butterfly 🦋 (low) or eagle 🦅 (high) logo.\n"
    "- Net worth calculation across all connected accounts.\n"
    "- Crypto swap, send, and exchange trading.\n"
    "- P2P USDT to NGN conversion.\n"
    "- Bank statement generation for loans or visas.\n"
    "- Daily streak rewards (earn 33MB data per day logged).\n"
    "- Tax estimation and payment.\n\n"

    "LANGUAGE: You speak English, Pidgin, Yoruba, Hausa and Igbo fluently. "
    "If the user writes to you in Yoruba or Igbo, respond in the same language, "
    "keeping the same warm, friendly, and occasionally playful tone. "
    "Use short sentences and never sound like a textbook. "
    "Avoid phrases like 'As an AI, I cannot…' or 'It is important to note…'. "
    "Match the user's energy. Be encouraging, practical, and playful when appropriate."
)

onboarding_state = {}
pending_transfers = {}  # user_id -> payload
pending_p2p_trades = {}
app = Flask(__name__)
socketio.init_app(app)
CORS(app, resources={r"/*": {"origins": [
    "https://oyinda-web.onrender.com",
    "http://localhost:5173",
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

    # Step 1: Determine category if not known
    if state == "collecting_category" and not category:
        p["state"] = "collecting_category"
        return jsonify({
            "message": "What was this for? For example: food, transport, rent, data, health, education, savings, etc.",
            "tone": "neutral"
        })

    # Step 2: Category‑specific questions
    if category in ['food', 'transport', 'housing', 'utilities', 'health', 'education', 'saving', 'investment'] and state == "collecting_category":
        if category == 'food':
            p["state"] = "collecting_quantity"
            return jsonify({
                "message": "How much did you buy? For example: 2 mudu, 1 derica, a paint, 5 kg, 10 pieces.",
                "tone": "neutral"
            })
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
            p["state"] = "collecting_location"
            return ask_for_location(user_id)

    elif state == "collecting_quantity":
        # Try to extract multiple items: "1 loaf of bread, 5 oranges, 1 bottle of coke"
        reply_lower = reply.lower()
        items = re.findall(
            r'(\d+)\s*(?:loaf|loaves|pieces?|bags?|bottles?|cups?|heaps?|bundles?|baskets?|packets?|sachets?|tins?|cans?|cartons?|rolls?|bars?|sticks?|units?|kg|g|litres?|liters?|ml|mudu|derica|paint|kongo|olodo|milk\s?cup|rubber|basin|bowl|plate|wrap|parcel|scoop)?\s*(?:of\s+)?(\w+(?:\s+\w+)?)',
            reply_lower)
        if items:
            # Build a clean summary: "1 loaf bread, 5 oranges, 1 bottle coke"
            parts = []
            for qty, unit, name in items:
                unit = unit if unit else ''
                part = f"{qty} {unit} {name.strip()}".strip()
                parts.append(part)
            description = ", ".join(parts)

            p["data"]["quantity_description"] = description
            p["data"]["quantity"] = len(items)  # number of distinct items
            p["data"]["unit"] = "items"
            p["data"] = p["data"]
            p["state"] = "collecting_location"
            return ask_for_location(user_id)
        else:
            return jsonify({
                "message": "I didn't catch the quantity. You can say something like '2 mudu', '1 paint', or '1 loaf of bread, 5 oranges'.",
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

    if trans_type in ("expense", "spent", "loan"):
        event_type = "ExpenseLogged"
        category = data.get("category", "other")
    elif trans_type == "income":
        event_type = "IncomeReceived"
        category = "income"
    elif trans_type == "investment":
        event_type = "InvestmentMade"
        category = "investment"
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

    }
    payload = {k: v for k, v in payload.items() if v is not None}

    event = append_event(user_id, user_id, event_type, payload)
    # After a successful log, insert a clear system note into conversation history
    log_msg = f"Oyinda just recorded this transaction: {description} – ₦{amount:,.2f} ({event_type})"
    save_conversation(user_id, 'system', log_msg)
    name = get_user_name(user_id)
    response_text = f"Logged: {description} – ₦{amount:,.2f}"

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

# --------------- COMMAND HANDLER ---------------
@app.route('/command', methods=['POST'])
@jwt_required()
def handle_command():
    user_id = get_jwt_identity()
    data = request.get_json()
    text = data.get('text', '').strip()
    save_conversation(user_id, 'user', text)

    if not text:
        return jsonify({"error": "No text provided"}), 400

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

    # ---------- CONTINUE PENDING CONVERSATION ----------
    if user_id in pending_transaction:
        p = pending_transaction[user_id]
        state = p["state"]
        reply = text.strip()

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
            else:
                return jsonify({
                    "message": f"Sorry, was {p['data']['amount']} NGN spent, earned, invested, or a loan?",
                    "tone": "neutral"
                })
            p["state"] = "collecting_category"
            return ask_next_question(user_id)

        elif state == "collecting_loan_direction":
            reply_lower = reply.lower()
            if any(w in reply_lower for w in ['borrow', 'from', 'i go pay back', 'i will pay back']):
                # Liability – the user owes money
                p["data"]["type"] = "loan"
                p["data"]["category"] = "loan"
                p["state"] = "collecting_category"
                return ask_next_question(user_id)
            elif any(w in reply_lower for w in ['lend', 'lent', 'to', 'they go pay me', 'they will pay me']):
                # Asset – someone else owes the user
                p["data"]["type"] = "asset"
                p["data"]["category"] = "loan_given"
                p["state"] = "collecting_category"
                return ask_next_question(user_id)
            else:
                return jsonify({
                    "message": "Sorry, I didn’t understand. Did you borrow from someone, or did you lend to someone?",
                    "tone": "neutral"
                })


        elif state == "collecting_category":
            reply_lower = reply.lower()
            cat_map = {
                'food': ['food', 'rice', 'beans', 'garri', 'yam', 'meat', 'spaghetti'],
                'transport': ['transport', 'okada', 'uber', 'taxi', 'bus', 'keke', 'fuel'],
                'housing': ['rent', 'house', 'room', 'accommodation'],
                'utilities': ['data', 'internet', 'electricity', 'bill'],
                'health': ['doctor', 'medicine', 'hospital'],
                'education': ['school', 'book', 'course'],
                'investment': ['invest', 'investment', 'stocks', 'bamboo'],
                'savings': ['save', 'savings', 'saved', 'piggyvest'],
                'loan': ['borrow', 'loan']
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
                    "message": "I didn't recognise that category. Could you try again? (e.g., food, transport, rent, data)",
                    "tone": "neutral"
                })
            p["state"] = "collecting_category"   # let ask_next_question advance
            return ask_next_question(user_id)

        elif state == "collecting_quantity":
            qty_match = re.search(r'(\d+)\s*(mudu|derica|paint|kg|g|pieces|heap|basket|bag|litre|liter)?', reply.lower())
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

        elif state == "collecting_location":
            p["data"]["location"] = reply.strip()
            return finalise_transaction(user_id)


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
                # Fall through to the normal smart fallback

        # (Other states like collecting_transport_type, housing_type can be added later)

        # If we didn't match any state, just finalise to avoid hanging
        return finalise_transaction(user_id)

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



    # 3. Balance / budget / net worth / credit score / debt keywords
    if any(w in text_lower for w in ['balance', 'how much is in', 'how much in', 'budget', 'net worth', 'credit score',
                                     'health score', 'debt', 'owe', 'liability']):
        return handle_query(text, user_id)

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
    amounts = re.findall(r'(\d[\d,]*\.?\d*)', text)
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

    # ---------- SMART FALLBACK: detect number (supports "1k", "2.5K", etc.) → start conversation ----------
    amount_match = re.search(r'(\d[\d,]*\.?\d*)\s*(k|K)?', text)
    if amount_match:
        amount_str = amount_match.group(1).replace(',', '')
        try:
            amount = float(amount_str)
            if amount_match.group(2):  # 'k' or 'K' present → multiply by 1000
                amount *= 1000
            pending_transaction[user_id] = {
                "state": "collecting_type",
                "data": {
                    "amount": amount,
                    "description": text,
                    "currency": "NGN",
                    "category": None
                },
                "category": None
            }

            # Detect probable type from keywords
            if any(word in text.lower() for word in ['spent', 'bought', 'paid', 'expense', 'drop']):
                pending_transaction[user_id]["data"]["type"] = "expense"
                pending_transaction[user_id]["state"] = "collecting_category"
                return ask_next_question(user_id)
            elif any(word in text.lower() for word in ['earned', 'made', 'profit', 'income', 'received']):
                pending_transaction[user_id]["data"]["type"] = "income"
                pending_transaction[user_id]["state"] = "collecting_category"
                return ask_next_question(user_id)
            elif any(word in text.lower() for word in ['borrow', 'loan', 'lend']):
                # We need to clarify direction: borrowed FROM someone (liability) or lent TO someone (asset)
                pending_transaction[user_id]["state"] = "collecting_loan_direction"
                pending_transaction[user_id]["data"]["type"] = "loan"
                return jsonify({
                    "message": "Did you borrow this money from someone (you will pay it back), or did you lend it to someone (they will pay you)?",
                    "tone": "neutral"
                })

            elif any(word in text.lower() for word in ['saved', 'invested', 'save', 'invest', 'savings']):
                pending_transaction[user_id]["data"]["type"] = "investment"
                pending_transaction[user_id]["data"]["category"] = "investment"
                pending_transaction[user_id]["category"] = "investment"
                return ask_for_location(user_id)
            elif any(word in text.lower() for word in ['borrow', 'loan', 'lend']):
                pending_transaction[user_id]["data"]["type"] = "loan"
                pending_transaction[user_id]["state"] = "collecting_category"
                return ask_next_question(user_id)
            else:
                # No clear type – ask the basic question
                return jsonify({
                    "message": f"Did you spend, earn, invest, save, or take a loan of {amount:,.2f}? Please reply with one word.",
                    "tone": "neutral"
                })
        except ValueError:
            pass

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
    token = data.get('token', '')
    text = data.get('text', '').strip()

    conn = get_conn()
    cur = conn.cursor()

    # ---------- Start new session ----------
    if not token:
        token = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO onboarding_sessions (token, step, data) VALUES (%s, %s, %s)",
            (token, 'ask_new_or_returning', json.dumps({}))
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
        # Expired or invalid – start fresh
        cur.close()
        conn.close()
        token = str(uuid.uuid4())
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO onboarding_sessions (token, step, data) VALUES (%s, %s, %s)",
            (token, 'ask_new_or_returning', json.dumps({}))
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

    # ---------- STEP HANDLERS (each returns immediately) ----------

    # --- 1. NEW OR RETURNING ---
    if step == 'ask_new_or_returning':
        if any(word in text.lower() for word in ['login', 'returning', 'existing', 'already', 'have']):
            new_step = 'login_email'
        elif any(word in text.lower() for word in ['new', 'register', 'sign up', 'create']):
            new_step = 'ask_name'
        else:
            cur.close()
            conn.close()
            return jsonify({"message": "I didn't get that. Are you new here (type 'new') or do you already have an account (type 'login')?", "tone": "neutral"})

        cur.execute("UPDATE onboarding_sessions SET step = %s WHERE token = %s", (new_step, token))
        conn.commit()
        cur.close()
        conn.close()
        # Return the appropriate prompt for the new step
        prompts = {
            'login_email': "Welcome back! What's your email address?",
            'ask_name': "Great! Let's get you started. What's your full name?"
        }
        return jsonify({"message": prompts[new_step], "tone": "neutral"})

    # --- 2. LOGIN EMAIL ---
    if step == 'login_email':
        if '@' not in text or '.' not in text:
            cur.close()
            conn.close()
            return jsonify({"message": "That doesn't look like a valid email. Could you double‑check?", "tone": "neutral"})
        user_data['email'] = text.strip()
        cur.execute(
            "UPDATE onboarding_sessions SET step = 'login_password', data = %s WHERE token = %s",
            (json.dumps(user_data), token)
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"message": "Enter your password:", "tone": "neutral"})

    # --- 3. LOGIN PASSWORD ---
    if step == 'login_password':
        user_data['password'] = text
        cur.close()
        conn.close()
        from core import authenticate_user
        user = authenticate_user(user_data['email'], user_data['password'])
        if not user:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("DELETE FROM onboarding_sessions WHERE token = %s", (token,))
            conn.commit()
            cur.close()
            conn.close()
            return jsonify({"message": "Invalid email or password. Please try again later.", "tone": "warning"})
        # Login success
        access_token = create_access_token(identity=str(user['id']))
        conn = get_conn()
        cur = conn.cursor()
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

    # --- 4. ASK NAME (registration) ---
    if step == 'ask_name':
        user_data['name'] = text.strip()
        cur.execute(
            "UPDATE onboarding_sessions SET step = 'ask_email', data = %s WHERE token = %s",
            (json.dumps(user_data), token)
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"message": f"Nice to meet you, {user_data['name']}! What's your email address?", "tone": "neutral"})

    # --- 5. ASK EMAIL (registration) ---
    if step == 'ask_email':
        if '@' not in text or '.' not in text:
            cur.close()
            conn.close()
            return jsonify({"message": "That doesn't look like a valid email. Could you double‑check?", "tone": "neutral"})
        user_data['email'] = text.strip()
        cur.execute(
            "UPDATE onboarding_sessions SET step = 'ask_password', data = %s WHERE token = %s",
            (json.dumps(user_data), token)
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"message": "Create a password (minimum 6 characters):", "tone": "neutral"})

    # --- 6. ASK PASSWORD (registration) ---
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

    # --- 7. ASK TYPE ---
    if step == 'ask_type':
        user_type = text.strip().lower()
        if user_type not in ['individual', 'business', 'company']:
            cur.close()
            conn.close()
            return jsonify({"message": "Please choose one: individual, business, or company.", "tone": "neutral"})
        user_data['account_type'] = user_type
        if user_type in ['business', 'company']:
            new_step = 'ask_business_name'
        else:
            new_step = 'finalize'
        cur.execute(
            "UPDATE onboarding_sessions SET step = %s, data = %s WHERE token = %s",
            (new_step, json.dumps(user_data), token)
        )
        conn.commit()
        cur.close()
        conn.close()
        if new_step == 'finalize':
            return finalize_registration(token, user_data)
        return jsonify({"message": "What is the name of your business? (or type 'skip' to skip)", "tone": "neutral"})

    # --- 8. ASK BUSINESS NAME ---
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

    # --- 9. ASK BUSINESS ADDRESS ---
    if step == 'ask_business_address':
        if text.strip().lower() != 'skip':
            user_data['business_address'] = text.strip()
        cur.close()
        conn.close()
        return finalize_registration(token, user_data)

    # --- Fallback ---
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
        system_msg += "Respond as Oyinda. Keep it short, warm, and helpful. If the user seems confused about finances, gently guide them to log an expense or income."

        # Try Groq first, then OpenAI fallback
        print("CONVERSATIONAL_REPLY system_msg (first 200 chars):", system_msg[:200])
        reply = _call_llm("groq", system_msg)
        print("CONVERSATIONAL_REPLY groq reply:", reply)
        if reply:
            return reply
        reply = _call_llm("openai", system_msg)
        print("CONVERSATIONAL_REPLY openai reply:", reply)
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
                    "temperature": 0.8,
                    "top_p": 0.9,
                    "max_tokens": 200
                },
                timeout=15
            )
        else:  # openai
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY')}"},
                json={
                    "model": "gpt-4o-mini",   # correct model name
                    "messages": [{"role": "system", "content": system_msg}],
                    "temperature": 0.8,
                    "top_p": 0.9,
                    "max_tokens": 200
                },
                timeout=15
            )
        data = resp.json()
        print(f"CALL_LLM {provider} status: {resp.status_code}, body: {str(data)[:300]}")
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

        amount = payload.get('amount', 0) or 0  # guard against None
        currency = payload.get('currency', 'NGN')

        # ---------- Determine the real institution ----------
        inst_id = stream_id
        inst_name = institutions.get(inst_id, {}).get('name', None)

        # For executed crypto/trade events, the real institution is in the original request event
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

        # If still unknown, label as Informal
        if not inst_name:
            inst_name = 'Informal'
            inst_id = 'manual'

        # Ensure a running balance exists for this institution
        running_balances.setdefault(inst_id, 0)

        # Classify the transaction
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

    # ---------- 5. Generate Markdown ----------
    md = f"# OFFICIAL BANK & ASSET STATEMENT\n\n"
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
        sb = 0  # Starting balance before period – can be improved later
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
    md += "**Official Verification:** This statement was generated by Oyinda and is valid for financial review.\n"
    md += f"**Verification Hash:** `{hashlib.sha256(md.encode()).hexdigest()[:12]}`\n"
    md += "**Disclaimer:** This is a computer-generated document. No signature required.\n"

    # ---------- 6. Output ----------
    if fmt == 'pdf':
        try:
            import markdown
            from weasyprint import HTML

            # Add CSS styling for the statement
            styled_html = f"""
            <html>
            <head>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    margin: 40px;
                    color: #333;
                }}
                h1 {{
                    color: #d63384;  /* light pink */
                }}
                h2 {{
                    color: #495057;
                    border-bottom: 2px solid #e9ecef;
                    padding-bottom: 5px;
                }}
                table {{
                    width: 100%;
                    border-collapse: collapse;
                    margin: 20px 0;
                }}
                th, td {{
                    border: 1px solid #dee2e6;
                    padding: 8px;
                    text-align: left;
                }}
                th {{
                    background-color: #f8f9fa;  /* light grey */
                    font-weight: bold;
                }}
                tr:nth-child(even) {{
                    background-color: #f8f9fa;
                }}
                tr:nth-child(odd) {{
                    background-color: #ffffff;
                }}
                .footer {{
                    margin-top: 40px;
                    font-size: 12px;
                    color: #6c757d;
                }}
                .verification {{
                    background-color: #fff3f3;
                    padding: 10px;
                    border-radius: 5px;
                }}
            </style>
            </head>
            <body>
            {markdown.markdown(md, extensions=['tables'])}
            </body>
            </html>
            """
            buffer = io.BytesIO()
            HTML(string=styled_html).write_pdf(buffer)
            buffer.seek(0)
            return send_file(buffer, as_attachment=True, download_name=f'oyinda_statement_{from_date}_{to_date}.pdf')
        except ImportError:
            return jsonify({"error": "PDF generation libraries not installed."}), 500



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


# --------------- HEALTH ---------------
@app.route('/health', methods=['GET'])
@jwt_required()
def health():
    user_id = get_jwt_identity()
    score_data = get_credit_score(user_id)
    score = score_data["score"]
    # Plain‑language description
    if score < 40:
        desc = "Keep logging to build your score"
    elif score < 70:
        desc = "Doing well – regular saving helps"
    elif score < 90:
        desc = "Great financial health!"
    else:
        desc = "Excellent! You’re an eagle"
    return jsonify({"score": score, "logo": score_data["logo"], "description": desc})

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
    conn = get_conn()
    cur = conn.cursor()

    # Get total income in last 12 months (or this year)
    cur.execute("""
        SELECT SUM(amount) FROM transactions_view
        WHERE user_id=%s AND type='income'
          AND date >= DATE_TRUNC('year', NOW())
    """, (user_id,))
    yearly_income = cur.fetchone()[0] or 0
    conn.close()

    # Nigerian presumptive tax (simplified): flat rate based on income bracket
    # This is a rough approximation; real tax laws are more complex.
    if yearly_income <= 300000:
        tax = 0
    elif yearly_income <= 600000:
        tax = (yearly_income - 300000) * 0.07
    elif yearly_income <= 12000000:
        tax = (yearly_income - 600000) * 0.15 + 300000 * 0.07
    elif yearly_income <= 30000000:
        tax = (yearly_income - 12000000) * 0.25 + 18000000 * 0.15 + 300000 * 0.07
    else:
        tax = (yearly_income - 30000000) * 0.30 + 6000000 * 0.25 + 18000000 * 0.15 + 300000 * 0.07

    tax = round(tax, 2)

    return jsonify({
        "yearly_income": round(yearly_income, 2),
        "estimated_tax": tax,
        "currency": "NGN",
        "note": "This is an estimate based on your logged income. Please consult a tax professional."
    })

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

    # Get consecutive days from today backwards
    today = datetime.utcnow().date()
    streak = 0
    while True:
        cur.execute("SELECT 1 FROM daily_activity_log WHERE user_id = %s AND date = %s", (user_id, today))
        if cur.fetchone():
            streak += 1
            today = today - timedelta(days=1)
        else:
            break
    conn.close()

    # Check if reward already given this month
    first_of_month = datetime.utcnow().replace(day=1).date()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM data_rewards WHERE user_id = %s AND awarded_at >= %s", (user_id, first_of_month))
    already_rewarded = cur.fetchone() is not None
    conn.close()

    return jsonify({
        "streak": streak,
        "next_reward_at": 30,
        "eligible": streak >= 30 and not already_rewarded
    })


@app.route('/cron/remind', methods=['POST'])
def cron_remind():
    secret = request.headers.get('X-Cron-Secret') or request.args.get('secret')
    if secret != CRON_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    # Find users who haven't logged any event today
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

    for user in users:
        # Log reminder – replace with real email sending later
        cur.execute(
            "INSERT INTO reminder_log (user_id, email) VALUES (%s, %s)",
            (user[0], user[1])
        )
        # TODO: send actual email using SendGrid / SMTP
        # send_email(user[2], user[1], "Don't forget to log your transactions today!")

    conn.commit()
    conn.close()
    return jsonify({"reminders_sent": len(users)})


@app.route('/feedback', methods=['POST'])
@jwt_required()
def submit_feedback():
    user_id = get_jwt_identity()
    data = request.get_json()
    message = data.get('message')
    if not message or not message.strip():
        return jsonify({"error": "Message required"}), 400

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO feedback (user_id, message) VALUES (%s, %s)",
        (user_id, message.strip())
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



def finalize_registration(token, user_data):
    from core import create_user
    user_id = create_user(
        name=user_data.get('name', ''),
        email=user_data.get('email', ''),
        password=user_data.get('password', ''),
        account_type=user_data.get('account_type', 'personal'),
        address=user_data.get('business_address', '')
    )
    if not user_id:
        # Email already exists – go back to email step
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE onboarding_sessions SET step = 'ask_email' WHERE token = %s", (token,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"message": "That email is already registered. Please enter a different email address:", "tone": "neutral"})

    # Clean up session
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM onboarding_sessions WHERE token = %s", (token,))
    conn.commit()
    cur.close()
    conn.close()

    if user_data.get('account_type') in ['business', 'company']:
        store_user_fact(user_id, 'business_name', user_data.get('business_name', ''))
        store_user_fact(user_id, 'business_address', user_data.get('business_address', ''))

    access_token = create_access_token(identity=str(user_id))
    reply = onboarding_message(token, 'confirm', user_data, None)
    return jsonify({
        "token": token,
        "jwt": access_token,
        "user": {"id": user_id, "name": user_data['name']},
        "message": reply or f"All set, {user_data['name']}! You're now registered. Redirecting to your dashboard…",
        "tone": "income",
        "redirect": "/dashboard"
    })


if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)