# app.py – Oyinda V2 API (Final: voice, statements, swap, credit, bank linking)

import os, re, uuid, requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory, send_file
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


pending_transfers = {}  # user_id -> payload
pending_p2p_trades = {}
app = Flask(__name__)
CORS(app)
app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'change-me-in-production-please')
jwt = JWTManager(app)
temp_links = {}

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
    return jsonify({"message": f"Welcome {name}! I'm your CFO. Let's build your financial future.", "user": {"id": user_id, "name": name}, "token": token})

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
    if not text:
        return jsonify({"error": "No text provided"}), 400

    # Check for pending transfer confirmation
    if text.strip().lower() in ['yes', 'confirm', 'confirm transfer', 'ok', 'approve']:
        pending = pending_transfers.get(user_id)
        if pending:
            append_event(user_id, user_id, 'TransferConfirmed', pending['payload'])
            success, ref = mock_execute_transfer(pending['payload'])
            if success:
                append_event(user_id, user_id, 'TransferExecuted', {**pending['payload'], "reference": ref})
                del pending_transfers[user_id]
                return jsonify({
                    "message": f"Transfer of {pending['payload']['amount']} {pending['payload']['currency']} completed.",
                    "tone": "income"})
            else:
                append_event(user_id, user_id, 'TransferFailed', {**pending['payload'], "error": ref})
                del pending_transfers[user_id]
                return jsonify({"error": f"Transfer failed: {ref}"}), 500

    # Check for pending P2P trade confirmation
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
                # Append event
                append_event(user_id, p2p_account_id, 'P2PSellExecuted', {
                    "amount": trade['amount'],
                    "currency": trade['currency'],
                    "ngn_equivalent": trade['ngn_amount'],
                    "rate": trade['rate'],
                    "order_id": result.get('result', {}).get('orderId')
                })
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
                return jsonify({
                                   "message": f"Bought {trade['crypto_amount']:.4f} {trade['currency']} for ₦{trade['amount']:,.2f}. P2P order created.",
                                   "tone": "income"})
        except Exception as e:
            return jsonify({"error": f"P2P trade failed: {str(e)}"}), 500


    # ---------- Rule‑based swap detector (fast path) ----------
    swap_match = re.match(r'swap\s+(\d+\.?\d*)\s*(\w+)\s+(?:for|to)\s+(\w+)\s+(?:on|in|using|from)?\s*(.*)', text, re.IGNORECASE)
    if swap_match:
        amount = float(swap_match.group(1))
        token_in = swap_match.group(2).upper()
        token_out = swap_match.group(3).upper()
        wallet_name = swap_match.group(4).strip().lower() or 'metamask'

        # Find the wallet account
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
        return jsonify({
            "message": f"Swapping {amount} {token_in} for {token_out} on {wallet_account['label']}. Confirm in your wallet.",
            "tone": "neutral",
            "event_id": event['event_id'],
            "requires_confirmation": True,
            "swap_payload": swap_payload
        })


    # ========== RULE-BASED FALLBACK (covers 90% of user intents) ==========
    text_lower = text.lower().strip()

    # 1. Catch ALL “how much …” / “what is my …” questions → query handler
    if text_lower.startswith(('how much', 'what is my', 'whats my', 'what are my', 'how many', 'what is the')):
        return handle_query(text, user_id)

    # 2. Exact greetings only (do not use substring matches)
    if text_lower in ['hello', 'hi', 'hey', 'good morning', 'good evening', 'help', 'what can you do']:
        name = get_user_name(user_id)
        return jsonify(
            {"answer": f"Hi {name}! I'm Oyinda, your personal CFO. How can I help you today?", "tone": "neutral"})

    # 2b. Link bank command
    if text_lower in ['link bank', 'link my bank', 'connect bank', 'add bank account']:
        return jsonify({"open_mono": True, "message": "Opening bank connection…"})

    # 3. Balance / budget / net worth / credit score / debt keywords
    if any(w in text_lower for w in ['balance', 'how much is in', 'how much in', 'budget', 'net worth', 'credit score',
                                     'health score', 'debt', 'owe', 'liability']):
        return handle_query(text, user_id)

    # 4. Swap (crypto)
    swap_match = re.match(r'swap\s+(\d+\.?\d*)\s*(\w+)\s+(?:for|to)\s+(\w+)\s+(?:on|in|using|from)?\s*(.*)', text,
                          re.IGNORECASE)
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
        return jsonify({
            "message": f"Swapping {amount} {token_in} for {token_out} on {wallet_account['label']}. Confirm in your wallet.",
            "tone": "neutral",
            "event_id": event['event_id'],
            "requires_confirmation": True,
            "swap_payload": swap_payload
        })

    # 4b. Exchange trade (buy/sell on Binance, Bybit, etc.)
    trade_match = re.match(r'(buy|sell)\s+(\d+\.?\d*)\s*(\w+)\s+(?:on|using|with|from)?\s*(\w+)', text, re.IGNORECASE)
    if trade_match:
        action = trade_match.group(1).lower()
        amount = float(trade_match.group(2))
        symbol = trade_match.group(3).upper()
        exchange_name = trade_match.group(4).lower()

        # Auto‑append USDT if the symbol looks like a standalone asset
        common_assets = {'BTC', 'ETH', 'BNB', 'XRP', 'SOL', 'ADA', 'AVAX', 'LINK', 'DOT', 'LTC', 'BCH', 'ATOM', 'UNI',
                         'ETC', 'FIL', 'APT', 'ARB', 'OP', 'NEAR', 'MATIC'}  # expand as needed
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

    # 5. Send token (crypto)
    send_match = re.match(r'send\s+(\d+\.?\d*)\s*(\w+)\s+to\s+(0x[a-fA-F0-9]+)\s+(?:from|using|on)?\s*(.*)', text,
                          re.IGNORECASE)
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
        return jsonify({
            "message": f"Sending {amount} {token} to {to_address} from {wallet_account['label']}. Confirm in your wallet.",
            "tone": "neutral",
            "event_id": event['event_id'],
            "requires_confirmation": True,
            "send_payload": send_payload
        })



    #5b ---------- SELL USDT via MONICA (automated) ----------
    sell_monica_match = re.match(r'sell\s+(\d+\.?\d*)\s*(USDT|USDC)\s+(?:for|to)\s*(?:ngn|naira)(?:\s*via\s*monica)?',
                                 text, re.IGNORECASE)
    if not sell_monica_match:
        sell_monica_match = re.match(r'convert\s+(\d+\.?\d*)\s*(USDT|USDC)\s+to\s+(?:ngn|naira)', text, re.IGNORECASE)
    if sell_monica_match:
        amount = float(sell_monica_match.group(1))
        currency = sell_monica_match.group(2).upper()

        # 1. Find Monica account
        accounts = get_user_connected_accounts(user_id)
        monica_account = next((a for a in accounts if a.get('provider', '').lower() == 'monica'), None)
        if not monica_account:
            return jsonify({"error": "No Monica account linked. Please link it under P2P."}), 400

        # 2. Get Monica deposit address
        try:
            from connectors.monica import MonicaConnector
            api_key = decrypt(monica_account['api_key_encrypted'])
            connector = MonicaConnector(api_key)
            deposit_address = connector.get_deposit_address("TRC20")  # or BEP20
            if not deposit_address:
                return jsonify({"error": "Could not get Monica deposit address."}), 500
        except Exception as e:
            return jsonify({"error": f"Monica API error: {str(e)}"}), 500

        # 3. Build the token‑transfer payload for the user’s BSC wallet
        wallet_accounts = [a for a in accounts if a['type'] == 'wallet']
        if not wallet_accounts:
            return jsonify({"error": "No connected crypto wallet."}), 400
        wallet_account = wallet_accounts[0]  # use the first wallet; could let user choose

        # Return a special response that the frontend will turn into a MetaMask transaction
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

    # 6. Expense logging
    expense_patterns = [
        r'(?:i\s+)?spent\s+(\d+\.?\d*)\s*(?:on\s+)?(.+)',
        r'(?:i\s+)?bought\s+(\d+\.?\d*)\s*(?:of\s+)?(.+)',
        r'(?:i\s+)?paid\s+(\d+\.?\d*)\s+(?:for\s+)?(.+)',
        r'i\s+drop\s+(\d+\.?\d*)\s+(?:for\s+|on\s+)?(.+)'  # pidgin
    ]
    expense_match = None
    for pat in expense_patterns:
        expense_match = re.match(pat, text, re.IGNORECASE)
        if expense_match:
            break

    if expense_match:
        amount = float(expense_match.group(1))
        description = expense_match.group(2).strip().lower()
        # Guess category from description
        cat_map = {
            'food': 'food', 'rice': 'food', 'beans': 'food', 'spaghetti': 'food', 'maggi': 'food',
            'transport': 'transport', 'uber': 'transport', 'taxi': 'transport', 'okada': 'transport',
            'fuel': 'transport',
            'data': 'utilities', 'internet': 'utilities', 'net': 'utilities', 'electricity': 'utilities',
            'bill': 'utilities',
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

    # 7. Income logging
    income_patterns = [
        r'(?:i\s+)?made\s+(\d+\.?\d*)\s*(?:profit|income|from|of)?\s*(.*)',
        r'(?:i\s+)?earned\s+(\d+\.?\d*)\s*(?:from\s+)?(.+)',
        r'(?:i\s+)?received\s+(\d+\.?\d*)\s*(?:from\s+)?(.+)',
        r'i\s+get\s+(\d+\.?\d*)\s+(?:from\s+)?(.+)'  # pidgin
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
        return jsonify({"message": response_text, "tone": "income", "event_id": event['event_id']})

    # 8. Bank transfer
    transfer_match = re.match(r'(?:send|transfer)\s+(\d+\.?\d*)\s+to\s+(?:account\s+)?(\d+)\s*(?:,?\s*(\w+\s*bank))?',
                              text,
                              re.IGNORECASE)
    if transfer_match:
        amount = float(transfer_match.group(1))
        dest_account = transfer_match.group(2)
        bank_name = transfer_match.group(3).strip() if transfer_match.group(3) else 'bank'
        accounts = get_user_connected_accounts(user_id)
        if not accounts:
            return jsonify({"error": "No connected accounts."}), 400
        source_id = accounts[0]['id']
        dest_id = accounts[0]['id']  # default
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
        return jsonify({"message": msg, "tone": "neutral", "event_id": event['event_id']})





    try:
        parsed = parse_intent_groq(text)
        if not parsed:
            # Fallback: if the AI parser fails, try the rule‑based query handler
            return handle_query(text, user_id)

        event_type = parsed.get('type')
        if event_type == 'question':
            return handle_query(text, user_id)

        # Allowed types – include send_token and swap
        if event_type not in ('expense', 'income', 'transfer', 'liability', 'asset', 'intention', 'buy', 'sell', 'swap', 'send_token'):
            return jsonify({"error": "I'm not sure how to handle that request."}), 400

        amount = parsed.get('amount')
        currency = parsed.get('currency', 'NGN')
        category = parsed.get('category', 'other')
        description = parsed.get('description', text)
        raw_date = parsed.get("date")
        date = normalize_date(raw_date) if raw_date else datetime.utcnow().strftime("%Y-%m-%d")

        # ---------- DEX SWAP (must come before CEX) ----------
        if event_type == 'swap':
            token_in = parsed.get('token_in', '')
            token_out = parsed.get('token_out', '')
            wallet_name = parsed.get('wallet', 'metamask').lower()

            accounts = get_user_connected_accounts(user_id)
            wallet_account = None
            for acc in accounts:
                if acc['type'] == 'wallet' and wallet_name in acc['label'].lower():
                    wallet_account = acc
                    break
            # If no exact match, fallback to first wallet
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
            return jsonify({
                "message": f"Swapping {amount} {token_in} for {token_out} on {wallet_account['label']}. Confirm in your wallet.",
                "tone": "neutral",
                "event_id": event['event_id'],
                "requires_confirmation": True,
                "swap_payload": swap_payload
            })

        # ---------- SEND TOKEN (must come before CEX) ----------
        if event_type == 'send_token':
            token = parsed.get('token', '')
            to_address = parsed.get('to_address', '')
            wallet_name = parsed.get('wallet', 'metamask').lower()

            accounts = get_user_connected_accounts(user_id)
            wallet_account = None
            for acc in accounts:
                if acc['type'] == 'wallet' and wallet_name in acc['label'].lower():
                    wallet_account = acc
                    break
            # If no exact match, fallback to first wallet
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
            return jsonify({
                "message": f"Sending {amount} {token} to {to_address} from {wallet_account['label']}. Confirm in your wallet.",
                "tone": "neutral",
                "event_id": event['event_id'],
                "requires_confirmation": True,
                "send_payload": send_payload
            })

        # Exchange trade (buy/sell on any linked exchange)
        trade_match = re.match(r'(buy|sell)\s+(\d+\.?\d*)\s*(\w+)\s+(?:on|using|with|from)?\s*(\w+)', text,
                               re.IGNORECASE)
        if trade_match:
            action = trade_match.group(1).lower()
            amount = float(trade_match.group(2))
            symbol = trade_match.group(3).upper()
            exchange_name = trade_match.group(4).lower()
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
                return jsonify(
                    {"message": f"{action.capitalize()} {amount} {symbol} on {ex_account['label']} submitted.",
                     "tone": "income"})
            except Exception as e:
                return jsonify({"error": f"Trade failed: {str(e)}"}), 500

        # ---------- BANK TRANSFER ----------
        if event_type == 'transfer':
            source_id = parsed.get("source_account_id")
            dest_id = parsed.get("destination_account_id")
            accounts = get_user_connected_accounts(user_id)
            if not accounts:
                return jsonify({"error": "No connected accounts."}), 400
            if not source_id:
                source_id = accounts[0]['id']
            if not dest_id:
                dest_id = accounts[0]['id']
            payload = {
                "amount": amount,
                "currency": currency,
                "date": date,
                "description": description,
                "source_account_id": source_id,
                "destination_account_id": dest_id
            }
            event = append_event(user_id, user_id, 'TransferRequested', payload)
            pending_transfers[user_id] = {"event_id": event['event_id'], "payload": payload}
            src_label = next((a['label'] for a in accounts if a['id'] == source_id), "your account")
            dst_label = next((a['label'] for a in accounts if a['id'] == dest_id), "the destination")
            msg = f"Okay, I'll send {amount} {currency} from {src_label} to {dst_label}. Please confirm this transfer."
            return jsonify({"message": msg, "tone": "neutral", "event_id": event['event_id']})

        # Missing amount guard (only for expense/income etc.)
        if event_type not in ('question', 'transfer', 'buy', 'sell', 'swap', 'send_token') and not amount:
            return jsonify({"error": "I didn't catch the amount. Please say something like 'I spent 500 on food'."}), 400

        # ---------- INCOME / EXPENSE / ASSET / LIABILITY / GOAL ----------
        if event_type == 'intention':
            payload = {
                "amount": amount,
                "currency": currency,
                "date": date,
                "description": description,
                "goal_type": parsed.get("goal_type", "general"),
                "deadline": parsed.get("deadline"),
                "target_amount": amount
            }
            event = append_event(user_id, user_id, 'GoalSet', payload)
            return jsonify({"message": f"Goal set! You want to save {amount} {currency} for {description}.", "tone": "income", "event_id": event['event_id']})

        if event_type == 'expense':
            final_type = 'ExpenseLogged'
        elif event_type == 'income':
            final_type = 'IncomeReceived'
        elif event_type == 'liability':
            final_type = 'ExpenseLogged'
        elif event_type == 'asset':
            final_type = 'ExpenseLogged'
            category = 'loan_given'
            parsed['category'] = category
        else:
            final_type = event_type

        payload = {"amount": amount, "currency": currency, "category": category, "date": date, "description": description}
        event = append_event(user_id, user_id, final_type, payload)

        name = get_user_name(user_id)
        tone = "neutral"
        if final_type == 'ExpenseLogged':
            if category == 'loan_given':
                response_text = f"Understood, {name}. You lent {amount} {currency}. I'll track this as an asset someone owes you."
            else:
                response_text = f"Got it, {name}. You spent {amount} {currency} on {category}."
                budget = calculate_daily_budget(user_id)
                if budget:
                    total_budget = sum(budget.values())
                    daily_limit = total_budget / len(budget) if len(budget) > 0 else 0
                    tone = "warning" if amount > daily_limit else "good"
        elif final_type == 'IncomeReceived':
            response_text = f"Great, {name}! You received {amount} {currency}. That's a step forward."
            tone = "income"
        else:
            response_text = f"Logged: {text}"
        return jsonify({"message": response_text, "tone": tone, "event_id": event['event_id']})

    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)}"}), 500




# --------------- QUERY HANDLER (with voice-friendly responses) ---------------
def handle_query(text, user_id):
    query_info = classify_query_intent(text)
    text_lower = text.lower()

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

    if 'liability' in text_lower or 'debt' in text_lower:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT SUM(amount) FROM transactions_view WHERE user_id=%s AND type='expense' AND category='loan'",
                    (user_id,))
        total = cur.fetchone()[0] or 0
        conn.close()
        return jsonify({"answer": f"Your total liability (loans taken) is ₦{total:,.2f}.", "tone": "neutral"})

    if 'invest' in text_lower or 'investment' in text_lower:
        # We need a category 'investment' – you can add it when logging.
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

    return jsonify({"answer": "I can help with budgets, spending, income, credit score, net worth, and accounts. Try asking 'how much did I spend on food this month?'", "tone": "neutral"})

# --------------- STATEMENT (PDF/JSON) ---------------
@app.route('/statement', methods=['GET'])
@jwt_required()
def generate_statement():
    user_id = get_jwt_identity()
    from_date = request.args.get('from', '1900-01-01')
    to_date = request.args.get('to', datetime.utcnow().strftime('%Y-%m-%d'))
    fmt = request.args.get('format', 'json')
    accounts = request.args.get('accounts', 'all')

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT date, type, amount, currency, category, description
        FROM transactions_view
        WHERE user_id=%s AND date BETWEEN %s AND %s
        ORDER BY date ASC
    """, (user_id, from_date, to_date))
    rows = cur.fetchall()
    conn.close()

    tx_list = [{"date": r[0], "type": r[1], "amount": r[2], "currency": r[3], "category": r[4], "description": r[5]} for r in rows]

    if fmt == 'json':
        return jsonify({"statement": {"from": from_date, "to": to_date, "transactions": tx_list}})
    elif fmt == 'pdf':
        try:
            from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.lib import colors
            buffer = BytesIO()
            doc = SimpleDocTemplate(buffer, pagesize=A4)
            elements = []
            styles = getSampleStyleSheet()
            title = Paragraph(f"Oyinda Statement ({from_date} to {to_date})", styles['Title'])
            elements.append(title)
            data = [['Date','Type','Amount','Currency','Category','Description']]
            for r in rows:
                data.append([r[0][:10], r[1], f"₦{r[2]:,.2f}", r[3], r[4], r[5][:40]])
            table = Table(data)
            table.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), colors.grey),
                ('TEXTCOLOR', (0,0), (-1,0), colors.whitesmoke),
                ('ALIGN', (0,0), (-1,-1), 'CENTER'),
                ('FONTSIZE', (0,0), (-1,-1), 8),
                ('GRID', (0,0), (-1,-1), 1, colors.black)
            ]))
            elements.append(table)
            doc.build(elements)
            buffer.seek(0)
            return send_file(buffer, as_attachment=True, download_name=f'statement_{from_date}_{to_date}.pdf')
        except ImportError:
            return jsonify({"error": "PDF generation not available (reportlab missing)."}), 500
    else:
        return jsonify({"error": "Unsupported format"}), 400



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


@app.route('/account/<account_id>', methods=['DELETE'])
@jwt_required()
def delete_account(account_id):
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


# --------------- HEALTH ---------------
@app.route('/health', methods=['GET'])
@jwt_required()
def health():
    user_id = get_jwt_identity()
    score = get_credit_score(user_id)
    return jsonify(score)

@app.route('/debug/binance', methods=['GET'])
@jwt_required()
def debug_binance():
    user_id = get_jwt_identity()
    accounts = get_user_connected_accounts(user_id)
    binance_account = next((a for a in accounts if a['provider'] == 'binance'), None)
    if not binance_account:
        return jsonify({"error": "No Binance account linked."}), 404

    try:
        from connectors.balances import get_account_balance
        result = get_account_balance(binance_account)
        return jsonify({"balance": result})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e), "type": type(e).__name__})



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


# --------------- FRONTEND ---------------
@app.route('/')
def landing():
    return send_from_directory('webapp', 'landing.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)