# app.py – Oyinda V2 API (non‑custodial, conversational)

import os
import re
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
from core import *
from groq_parser import parse_intent_groq, classify_query_intent
from utils.crypto import encrypt, decrypt
from connectors.mono import exchange_code, get_account_details, get_transactions, initiate_transfer

app = Flask(__name__)
CORS(app)
app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'change-me-in-production-please')
jwt = JWTManager(app)

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


def normalize_date(date_str):
    """Convert human-spoken dates like 'Monday', '3 days ago' to YYYY-MM-DD."""
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

    # Specific weekdays (last occurrence)
    weekdays = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday']
    if dl in weekdays:
        target_idx = weekdays.index(dl)
        current_idx = today.weekday()  # Monday=0
        days_back = (current_idx - target_idx) % 7
        if days_back == 0:
            days_back = 7  # "Monday" on Monday means last Monday
        return (today - timedelta(days=days_back)).strftime("%Y-%m-%d")

    # Common relative names
    if dl == 'yesterday':
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")
    if dl == 'today':
        return today.strftime("%Y-%m-%d")
    if dl == 'tomorrow':
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")  # unlikely but safe

    # fallback
    return today.strftime("%Y-%m-%d")

# --------------- COMMAND HANDLER ---------------
@app.route('/command', methods=['POST'])
@jwt_required()
def handle_command():
    user_id = get_jwt_identity()
    data = request.get_json()
    text = data.get('text', '').strip()
    if not text:
        return jsonify({"error": "No text provided"}), 400

    try:
        parsed = parse_intent_groq(text)
        if not parsed:
            return jsonify({"error": "I didn't understand that. Could you rephrase?"}), 400

        event_type = parsed.get('type')
        if event_type == 'question':
            return handle_query(text, user_id)

        if event_type not in ('expense', 'income', 'transfer', 'liability', 'asset', 'intention'):
            return jsonify({"error": "I'm not sure how to handle that request."}), 400

        amount = parsed.get('amount')
        currency = parsed.get('currency', 'NGN')
        category = parsed.get('category', 'other')
        description = parsed.get('description', text)

        # ---- Normalize the date once for all events ----
        raw_date = parsed.get("date")
        date = normalize_date(raw_date) if raw_date else datetime.utcnow().strftime("%Y-%m-%d")

        if event_type == 'transfer':
            source_id = parsed.get("source_account_id")
            dest_id = parsed.get("destination_account_id")
            accounts = get_user_connected_accounts(user_id)
            if not accounts:
                return jsonify({"error": "No connected accounts. Please link a bank or wallet first."}), 400
            if not source_id:
                source_id = accounts[0]['id']
            if not dest_id:
                dest_id = accounts[0]['id']
            payload = {
                "amount": amount,
                "currency": currency,
                "date": date,                # ✅ normalized
                "description": description,
                "source_account_id": source_id,
                "destination_account_id": dest_id
            }
            event = append_event(user_id, user_id, 'TransferRequested', payload)
            source_label = next((a['label'] for a in accounts if a['id'] == source_id), "your account")
            dest_label = next((a['label'] for a in accounts if a['id'] == dest_id), "the destination")
            response_text = f"Okay, I'll send {amount} {currency} from {source_label} to {dest_label}. Please confirm this transfer."
            return jsonify({"message": response_text, "tone": "neutral", "event_id": event['event_id']})

        # ---- Non‑transfer events ----
        if event_type == 'intention':
            payload = {
                "amount": amount,
                "currency": currency,
                "date": date,               # ✅ normalized
                "description": description,
                "goal_type": parsed.get("goal_type", "general"),
                "deadline": parsed.get("deadline"),
                "target_amount": amount
            }
            event = append_event(user_id, user_id, 'GoalSet', payload)
            response_text = f"Goal set! You want to save {amount} {currency} for {description}. I'll help you stay on track."
            return jsonify({"message": response_text, "tone": "income", "event_id": event['event_id']})

        if event_type in ('buy', 'sell', 'swap'):
            # Crypto trading order
            symbol = parsed.get('symbol', '')  # e.g., BTCUSDT
            side = event_type.upper()  # BUY or SELL
            quantity = parsed.get('amount')
            account_id = parsed.get('source_account_id')  # optional
            if not symbol or not quantity:
                return jsonify({"error": "Please specify asset pair and quantity."}), 400
            # If no account_id, use first exchange account
            if not account_id:
                exchange_accounts = [a for a in get_user_connected_accounts(user_id) if a['type'] == 'exchange']
                if not exchange_accounts:
                    return jsonify({"error": "No exchange account linked."}), 400
                account_id = exchange_accounts[0]['id']
            # Call the same /crypto/order logic or directly place order here
            # ... (omitted for brevity – you can reuse the connector)

        # income / expense / liability / asset
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

        payload = {
            "amount": amount,
            "currency": currency,
            "category": category,
            "date": date,                   # ✅ normalized, not parsed.get(...)
            "description": description
        }

        event = append_event(user_id, user_id, final_type, payload)

        name = get_user_name(user_id)
        # ---- Determine tone and response text ----
        tone = "neutral"  # default
        if final_type == 'ExpenseLogged':
            if category == 'loan_given':
                response_text = f"Understood, {name}. You lent {amount} {currency}. I'll track this as an asset someone owes you."
                tone = "neutral"
            else:
                response_text = f"Got it, {name}. You spent {amount} {currency} on {category}."
                # Check budget only for normal expenses
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



def get_user_name(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM users WHERE id=%s", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else "there"

# --------------- QUERY HANDLER (expanded) ---------------
def handle_query(text, user_id):
    # ---- GREETING DETECTION (via Groq classifier) ----
    query_info = classify_query_intent(text)
    if query_info and query_info.get('intent') == 'greeting':
        # Get user account type for personalised intro
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT name, account_type FROM users WHERE id=%s", (user_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            name, acct_type = row
            if acct_type == 'personal':
                intro = f"Hi {name}! I'm Oyinda, your personal assistant for financial inclusion. How can I help you today?"
            elif acct_type == 'business':
                intro = f"Hello {name}! I'm Oyinda, your business financial co‑pilot. Ready to optimise your cash flow?"
            elif acct_type == 'hni':
                intro = f"Good day {name}. I'm Oyinda, your private wealth coordinator. What shall we review today?"
            else:
                intro = f"Welcome back, {name}! I'm your AI CFO. How may I assist you?"
            return jsonify({"answer": intro, "tone": "neutral"})
        else:
            return jsonify({"answer": "Hello! I'm Oyinda, your financial companion. How can I assist?", "tone": "neutral"})

    # ---- SMART QUERIES (expense/income with date & category) ----
    text_lower = text.lower()
    if query_info:
        intent = query_info.get('intent')
        params = query_info.get('parameters', {})
        date_param = params.get('date')        # e.g. "last month", "this week"
        category = params.get('category')      # e.g. "gas", "food"

        if intent in ('expense', 'income') and (date_param or category):
            # Convert date parameter to actual date range
            start, end, label = extract_date_range(date_param or 'all time')
            conn = get_conn()
            cur = conn.cursor()
            type_filter = "type='expense'" if intent == 'expense' else "type='income'"
            cat_filter = ""
            query_params = [user_id, start, end]
            if category:
                cat_filter = " AND category = %s"
                query_params.append(category)
            cur.execute(f"SELECT SUM(amount) FROM transactions_view WHERE user_id=%s AND {type_filter} AND date BETWEEN %s AND %s{cat_filter}", query_params)
            total = cur.fetchone()[0] or 0
            conn.close()

            cat_text = f" on {category}" if category else ""
            return jsonify({"answer": f"Total {intent}{cat_text} for {label}: ₦{total:,.2f}", "tone": "neutral"})

    # ---- FALLBACK RULE‑BASED ----
    if any(w in text_lower for w in ['budget', 'limit', 'spend limit']):
        budget = calculate_daily_budget(user_id)
        if not budget:
            return jsonify({"answer": "I don't have enough data yet. Log some income and expenses first.", "tone": "neutral"})
        msg = "Here's your daily budget:\n" + "\n".join([f"• {k.replace('_',' ').title()}: ₦{v:,.2f}" for k,v in budget.items()])
        return jsonify({"answer": msg, "tone": "neutral"})

    if 'credit score' in text_lower or 'health score' in text_lower:
        score = get_credit_score(user_id)
        msg = f"Your financial health score is {score['score']}/100. You're a {score['logo']}."
        return jsonify({"answer": msg, "tone": "neutral"})

    # ---- Net worth ----
    if 'net worth' in text_lower or 'networth' in text_lower:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT SUM(amount) FROM transactions_view WHERE user_id=%s AND type='income'", (user_id,))
        total_income = cur.fetchone()[0] or 0
        cur.execute("SELECT SUM(amount) FROM transactions_view WHERE user_id=%s AND type='expense'", (user_id,))
        total_expense = cur.fetchone()[0] or 0
        conn.close()
        net = total_income - total_expense
        return jsonify({"answer": f"Your net worth (income minus expenses) is ₦{net:,.2f}.", "tone": "neutral"})

    # ---- Assets / connected accounts ----
    if any(w in text_lower for w in ['asset', 'account', 'wallet', 'bank', 'what do i own', 'what do i have']):
        accounts = get_user_connected_accounts(user_id)
        if not accounts:
            return jsonify({"answer": "You haven't linked any bank accounts or wallets yet.", "tone": "neutral"})
        msg = "Your connected accounts:\n" + "\n".join([f"• {a['label']} ({a['currency']})" for a in accounts])
        return jsonify({"answer": msg, "tone": "neutral"})

    # Simple date‑based expense/income queries (fallback)
    if any(w in text_lower for w in ['spent', 'expense', 'spend']):
        start, end, label = extract_date_range(text_lower)
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT SUM(amount) FROM transactions_view WHERE user_id=%s AND type='expense' AND date BETWEEN %s AND %s", (user_id, start, end))
        total = cur.fetchone()[0] or 0
        conn.close()
        return jsonify({"answer": f"Total expenses for {label}: ₦{total:,.2f}", "tone": "neutral"})

    if any(w in text_lower for w in ['made', 'earned', 'income', 'profit', 'generate', 'revenue']):
        start, end, label = extract_date_range(text_lower)
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT SUM(amount) FROM transactions_view WHERE user_id=%s AND type='income' AND date BETWEEN %s AND %s", (user_id, start, end))
        total = cur.fetchone()[0] or 0
        conn.close()
        return jsonify({"answer": f"Total income for {label}: ₦{total:,.2f}", "tone": "neutral"})



    return jsonify({"answer": "I can help with budgets, spending, income, or credit score. Try asking 'how much did I spend on food this month?'", "tone": "neutral"})



@app.route('/link/mono', methods=['POST'])
@jwt_required()
def link_mono():
    user_id = get_jwt_identity()
    data = request.get_json()
    code = data.get('code')
    if not code:
        return jsonify({"error": "Mono auth code required"}), 400

    try:
        mono_resp = exchange_code(code)
        mono_account_id = mono_resp['id']
        # Get account details
        details = get_account_details(mono_account_id)
        account_number = details.get('account_number', '')
        bank_name = details.get('institution', {}).get('name', 'Unknown Bank')
        currency = details.get('currency', 'NGN')

        # Encrypt access token (if any) – for security
        # Mono uses secret key, not individual tokens; we can store account id only.
        # If we later store a user token, encrypt it.

        payload = {
            "account_number": account_number,
            "bank_name": bank_name,
            "currency": currency,
            "mono_account_id": mono_account_id,
            "encrypted_token": ""   # not used for Mono now
        }

        # Append event
        event = append_event(user_id, user_id, 'BankAccountLinked', payload)

        return jsonify({"message": f"{bank_name} account ending {account_number[-3:]} linked successfully.",
                        "account_id": mono_account_id, "event_id": event['event_id']})
    except Exception as e:
        return jsonify({"error": f"Linking failed: {str(e)}"}), 500



@app.route('/sync/mono', methods=['POST'])
@jwt_required()
def sync_mono():
    user_id = get_jwt_identity()
    data = request.get_json()
    account_id = data.get('account_id')   # mono account id
    if not account_id:
        return jsonify({"error": "Mono account ID required"}), 400

    try:
        # Optional: from last sync date
        last_sync_date = get_last_sync_date(account_id)
        transactions = get_transactions(account_id, from_date=last_sync_date)
        count = 0
        for tx in transactions:
            # Idempotency: skip if we already have this transaction (by bank_ref)
            if is_transaction_processed(tx['id']):
                continue
            # Convert Mono transaction to our event
            tx_type = 'IncomeReceived' if tx['type'] == 'credit' else 'ExpenseLogged'
            tx_payload = {
                "amount": abs(tx['amount'] / 100),  # kobo to naira
                "currency": tx.get('currency', 'NGN'),
                "date": tx['date'][:10],
                "description": tx['narration'],
                "category": guess_category(tx['narration']),
                "bank_ref": tx['id']   # for idempotency
            }
            append_event(user_id, account_id, tx_type, tx_payload)
            count += 1

        update_last_sync_date(account_id)
        return jsonify({"message": f"Synced {count} new transactions."})
    except Exception as e:
        return jsonify({"error": f"Sync failed: {str(e)}"}), 500

# --------------- TRANSFER CONFIRMATION ---------------
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

    # Confirm and simulate execution
    append_event(user_id, stream_id, 'TransferConfirmed', payload)
    success, ref = mock_execute_transfer(payload)
    if success:
        append_event(user_id, stream_id, 'TransferExecuted', {**payload, "reference": ref})
        return jsonify({"message": f"Transfer of {payload['amount']} {payload['currency']} completed successfully.", "tone": "income"})
    else:
        append_event(user_id, stream_id, 'TransferFailed', {**payload, "error": ref})
        return jsonify({"message": f"Transfer failed: {ref}", "tone": "warning"})

def mock_execute_transfer(payload):
    # Replace with real payment gateway call
    return True, "MOCK-REF-001"


@app.route('/crypto/wallet/prepare', methods=['POST'])
@jwt_required()
def wallet_prepare():
    user_id = get_jwt_identity()
    data = request.get_json()
    action = data.get('action')  # 'transfer' or 'swap'
    wallet_account_id = data.get('account_id')
    if not action or not wallet_account_id:
        return jsonify({"error": "Missing action or account_id"}), 400

    # Validate wallet account belongs to user
    accounts = get_user_connected_accounts(user_id)
    wallet = next((a for a in accounts if a['id'] == wallet_account_id and a['type'] == 'wallet'), None)
    if not wallet:
        return jsonify({"error": "Wallet not found"}), 404

    # Build the transaction payload (simplified – in real code you'd craft the contract call)
    if action == 'transfer':
        to_address = data.get('to')
        amount = data.get('amount')  # in ETH/BNB
        if not to_address or not amount:
            return jsonify({"error": "to and amount required"}), 400
        tx_payload = {
            "from": wallet['wallet_address'],
            "to": to_address,
            "value": str(int(float(amount) * 1e18)),  # wei
            "chainId": 1,  # mainnet; adjust by network
        }
    elif action == 'swap':
        # Interact with Uniswap/Sushi – we'll provide a placeholder
        token_in = data.get('token_in')
        token_out = data.get('token_out')
        amount_in = data.get('amount_in')
        # In reality, you'd call the router's swapExactTokensForTokens
        tx_payload = {
            "from": wallet['wallet_address'],
            "to": "0xUNISWAP_ROUTER_ADDRESS",
            "data": "0x...",  # encoded swap function
            "value": "0",
            "chainId": 1
        }
    else:
        return jsonify({"error": "Unsupported action"}), 400

    # Return the payload to the frontend so WalletConnect can request signature
    return jsonify({"tx_payload": tx_payload})



@app.route('/crypto/wallet/submit', methods=['POST'])
@jwt_required()
def wallet_submit():
    user_id = get_jwt_identity()
    data = request.get_json()
    account_id = data.get('account_id')
    tx_hash = data.get('tx_hash')  # from wallet after broadcast
    # In a full implementation, we'd verify the signature, but for now we record the event
    payload = {"tx_hash": tx_hash, "account_id": account_id}
    append_event(user_id, account_id, 'WalletTransactionExecuted', payload)
    return jsonify({"message": "Transaction submitted and recorded."})


@app.route('/statement', methods=['GET'])
@jwt_required()
def generate_statement():
    user_id = get_jwt_identity()
    from_date = request.args.get('from', '1900-01-01')
    to_date = request.args.get('to', datetime.utcnow().strftime('%Y-%m-%d'))
    format = request.args.get('format', 'json')  # json or pdf
    accounts = request.args.get('accounts', 'all')

    conn = get_conn()
    cur = conn.cursor()
    # Get transactions
    if accounts == 'all':
        cur.execute("""
            SELECT date, type, amount, currency, category, description
            FROM transactions_view
            WHERE user_id=%s AND date BETWEEN %s AND %s
            ORDER BY date DESC
        """, (user_id, from_date, to_date))
    else:
        # Filter by specific account ids (more complex)
        cur.execute("""
            SELECT date, type, amount, currency, category, description
            FROM transactions_view
            WHERE user_id=%s AND date BETWEEN %s AND %s AND related_stream_id = ANY(%s)
            ORDER BY date DESC
        """, (user_id, from_date, to_date, accounts.split(',')))
    rows = cur.fetchall()
    conn.close()

    transactions = [
        {"date": row[0], "type": row[1], "amount": row[2], "currency": row[3], "category": row[4], "description": row[5]}
        for row in rows
    ]

    if format == 'json':
        return jsonify({"statement": {"from": from_date, "to": to_date, "transactions": transactions}})
    else:
        # PDF generation using ReportLab (install package first)
        from reportlab.lib.pagesizes import A4
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
        from io import BytesIO
        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4)
        elements = []
        # Build table...
        doc.build(elements)
        buffer.seek(0)
        return send_file(buffer, as_attachment=True, download_name='statement.pdf')


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

    transactions = [
        {"date": r[0], "type": r[1], "amount": r[2], "currency": r[3], "category": r[4], "description": r[5]}
        for r in rows
    ]
    return jsonify({"page": page, "transactions": transactions})


# --------------- HEALTH SCORE ---------------
@app.route('/health', methods=['GET'])
@jwt_required()
def health():
    user_id = get_jwt_identity()
    score = get_credit_score(user_id)
    return jsonify(score)


@app.route('/link/exchange', methods=['POST'])
@jwt_required()
def link_exchange():
    user_id = get_jwt_identity()
    data = request.get_json()
    provider = data.get('provider', 'binance').lower()
    if provider != 'binance':
        return jsonify({"error": "Only Binance supported for now"}), 400

    api_key = data.get('api_key')
    api_secret = data.get('api_secret')
    if not api_key or not api_secret:
        return jsonify({"error": "API key and secret required"}), 400

    try:
        # Test connection
        connector = BinanceConnector(api_key, api_secret)
        balances = connector.get_balances()  # minimal check
    except Exception as e:
        return jsonify({"error": f"Could not connect to Binance: {str(e)}"}), 400

    # Encrypt credentials
    enc_key = encrypt(api_key)
    enc_secret = encrypt(api_secret)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO connected_accounts (user_id, account_type, provider, label, currency, api_key_encrypted, api_secret_encrypted) VALUES (%s, 'exchange', %s, %s, 'USD', %s, %s) RETURNING id",
        (user_id, provider, f"{provider.capitalize()} Account", enc_key, enc_secret)
    )
    account_id = cur.fetchone()[0]
    conn.commit()
    conn.close()

    return jsonify({"message": f"{provider.capitalize()} account linked successfully.", "account_id": str(account_id)})


@app.route('/sync/exchange', methods=['POST'])
@jwt_required()
def sync_exchange():
    user_id = get_jwt_identity()
    data = request.get_json()
    account_id = data.get('account_id')
    if not account_id:
        return jsonify({"error": "account_id required"}), 400

    connector = get_exchange_connector(user_id, account_id)
    if not connector:
        return jsonify({"error": "Invalid account"}), 400

    try:
        balances = connector.get_balances()
        # For each balance, we could create a BalanceUpdated event, but we'll keep it simple: store as a summary in the connected_accounts metadata? For now, just return balances.
        # In a full implementation, you'd create events for each balance change.
        return jsonify({"balances": balances})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/crypto/order', methods=['POST'])
@jwt_required()
def crypto_order():
    user_id = get_jwt_identity()
    data = request.get_json()
    account_id = data.get('account_id')
    symbol = data.get('symbol')  # e.g., BTCUSDT
    side = data.get('side')  # BUY or SELL
    quantity = data.get('quantity')
    price = data.get('price')  # optional, for limit orders
    order_type = data.get('order_type', 'MARKET')

    if not all([account_id, symbol, side, quantity]):
        return jsonify({"error": "Missing required fields"}), 400

    connector = get_exchange_connector(user_id, account_id)
    if not connector:
        return jsonify({"error": "Invalid account"}), 400

    try:
        result = connector.place_order(symbol, side, quantity, price, order_type)
        # Append an event: CryptoOrderPlaced / Executed
        payload = {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "order_type": order_type,
            "order_id": result.get('orderId'),
            "price": result.get('price', '0')
        }
        append_event(user_id, account_id, 'CryptoOrderExecuted', payload)
        return jsonify({"message": f"Order placed: {side} {quantity} {symbol}", "order": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/crypto/withdraw', methods=['POST'])
@jwt_required()
def crypto_withdraw():
    user_id = get_jwt_identity()
    data = request.get_json()
    account_id = data.get('account_id')
    asset = data.get('asset')
    address = data.get('address')
    amount = data.get('amount')
    network = data.get('network')

    if not all([account_id, asset, address, amount]):
        return jsonify({"error": "Missing required fields"}), 400

    connector = get_exchange_connector(user_id, account_id)
    if not connector:
        return jsonify({"error": "Invalid account"}), 400

    try:
        result = connector.withdraw(asset, address, amount, network)
        payload = {"asset": asset, "address": address, "amount": amount, "network": network, "txid": result.get('id')}
        append_event(user_id, account_id, 'CryptoWithdrawalExecuted', payload)
        return jsonify({"message": f"Withdrawal of {amount} {asset} initiated."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route('/link/wallet', methods=['POST'])
@jwt_required()
def link_wallet():
    user_id = get_jwt_identity()
    data = request.get_json()
    address = data.get('address')
    network = data.get('network', 'TRC20')  # or ERC20, BTC, etc.
    label = data.get('label', f'{network} Wallet')

    if not address:
        return jsonify({"error": "Wallet address required"}), 400

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO connected_accounts (user_id, account_type, provider, label, currency, wallet_address, network) VALUES (%s, 'wallet', %s, %s, 'USD', %s, %s) RETURNING id",
        (user_id, network.lower(), label, address, network)
    )
    account_id = cur.fetchone()[0]
    conn.commit()
    conn.close()

    return jsonify({"message": f"{network} wallet linked. (Watch-only)", "account_id": str(account_id)})






# --------------- FRONTEND ---------------
@app.route('/')
def landing():
    return send_from_directory('webapp', 'landing.html')


def get_last_sync_date(account_id):
    # Retrieve from settings or a dedicated sync_tracker table
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE user_id=%s AND key=%s", (account_id, f"last_sync_{account_id}"))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def update_last_sync_date(account_id):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO settings (user_id, key, value) VALUES (%s, %s, %s) ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value",
        (account_id, f"last_sync_{account_id}", today)
    )
    conn.commit()
    conn.close()

def is_transaction_processed(bank_ref):
    # Check if an event with this bank_ref already exists (via metadata)
    # You can store in a separate table or check transactions_view
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM transactions_view WHERE description ILIKE %s", (f"%ref:{bank_ref}%",))
    exists = cur.fetchone()
    conn.close()
    return exists is not None

def guess_category(narration):
    narration = narration.lower()
    if any(w in narration for w in ['food', 'rice', 'beans', 'restaurant']):
        return 'food'
    if any(w in narration for w in ['uber', 'taxi', 'transport', 'fuel']):
        return 'transport'
    if any(w in narration for w in ['rent', 'housing']):
        return 'housing'
    if any(w in narration for w in ['electricity', 'water', 'utility', 'internet', 'data']):
        return 'utilities'
    if any(w in narration for w in ['salary', 'wage', 'payment received']):
        return 'income'
    return 'other'

def extract_date_range(date_param=None):
    today = datetime.utcnow().date()
    if not date_param:
        return "1900-01-01", today.strftime("%Y-%m-%d"), "all time"
    dl = date_param.lower()
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
        last_day_prev_month = first_day_this_month - timedelta(days=1)
        start_prev = last_day_prev_month.replace(day=1)
        return start_prev.strftime("%Y-%m-%d"), last_day_prev_month.strftime("%Y-%m-%d"), "last month"
    # fallback
    return "1900-01-01", today.strftime("%Y-%m-%d"), "all time"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)