# app.py – Oyinda V2 API (Final: voice, statements, swap, credit, bank linking)

import os, re, uuid
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory, send_file
from flask_cors import CORS
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
from io import BytesIO

from core import *
from groq_parser import parse_intent_groq, classify_query_intent
from utils.crypto import encrypt, decrypt
from connectors.mono import exchange_code, get_account_details, get_transactions, initiate_transfer
from connectors.exchange import BinanceConnector, get_exchange_connector


pending_transfers = {}  # user_id -> payload
app = Flask(__name__)
CORS(app)
app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'change-me-in-production-please')
jwt = JWTManager(app)

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

def extract_date_range(date_param):
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
        last_day_prev = first_day_this_month - timedelta(days=1)
        start_prev = last_day_prev.replace(day=1)
        return start_prev.strftime("%Y-%m-%d"), last_day_prev.strftime("%Y-%m-%d"), "last month"
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

    if text.strip().lower() in ['yes', 'confirm', 'confirm transfer', 'ok', 'approve']:
        pending = pending_transfers.get(user_id)
        if pending:
            # Execute the transfer
            # (call the confirm logic directly)
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

    try:
        parsed = parse_intent_groq(text)
        if not parsed:
            return jsonify({"error": "I didn't understand that. Could you rephrase?"}), 400
        event_type = parsed.get('type')
        if event_type == 'question':
            return handle_query(text, user_id)
        if event_type not in ('expense', 'income', 'transfer', 'liability', 'asset', 'intention', 'buy', 'sell', 'swap'):
            return jsonify({"error": "I'm not sure how to handle that request."}), 400

        amount = parsed.get('amount')
        currency = parsed.get('currency', 'NGN')
        category = parsed.get('category', 'other')
        description = parsed.get('description', text)
        raw_date = parsed.get("date")
        date = normalize_date(raw_date) if raw_date else datetime.utcnow().strftime("%Y-%m-%d")

        # Crypto trading
        if event_type in ('buy', 'sell', 'swap'):
            symbol = parsed.get('symbol', '')
            side = event_type.upper()
            quantity = amount
            accounts = get_user_connected_accounts(user_id)
            exchange_accts = [a for a in accounts if a['type'] == 'exchange']
            if not exchange_accts:
                return jsonify({"error": "No exchange account linked. Please connect Binance first."}), 400
            account_id = exchange_accts[0]['id']
            connector = get_exchange_connector(user_id, account_id)
            if not connector:
                return jsonify({"error": "Could not connect to exchange."}), 500
            try:
                order = connector.place_order(symbol, side, quantity)
                payload = {"symbol": symbol, "side": side, "quantity": quantity, "order_id": order.get('orderId'), "price": order.get('price','0')}
                event = append_event(user_id, account_id, 'CryptoOrderExecuted', payload)
                response_text = f"Order placed: {side} {quantity} {symbol}. Order ID: {order.get('orderId')}"
                return jsonify({"message": response_text, "tone": "income", "event_id": event['event_id']})
            except Exception as e:
                return jsonify({"error": f"Order failed: {str(e)}"}), 500

        # Transfer
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

        if event_type not in ('question', 'transfer', 'buy', 'sell', 'swap') and not amount:
            return jsonify(
                {"error": "I didn't catch the amount. Please say something like 'I spent 500 on food'."}), 400

        # Income / Expense / Asset / Liability / Goal
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

    # Net worth
    if 'net worth' in text_lower or 'networth' in text_lower:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT SUM(amount) FROM transactions_view WHERE user_id=%s AND type='income'", (user_id,))
        income = cur.fetchone()[0] or 0
        cur.execute("SELECT SUM(amount) FROM transactions_view WHERE user_id=%s AND type IN ('expense','transfer_executed','crypto_trade')", (user_id,))
        expense = cur.fetchone()[0] or 0
        conn.close()
        return jsonify({"answer": f"Your net worth (income minus expenses) is ₦{income - expense:,.2f}.", "tone": "neutral"})

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
    if any(w in text_lower for w in ['balance', 'how much is in', 'how much in', 'how many assets']):
        accounts = get_user_connected_accounts(user_id)
        if not accounts:
            return jsonify({"answer": "You haven't linked any accounts yet.", "tone": "neutral"})

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
        "INSERT INTO connected_accounts (user_id, account_type, provider, label, currency, wallet_address, network) VALUES (%s, 'wallet', %s, %s, 'ETH', %s, %s) RETURNING id",
        (user_id, network.lower(), label, address, network)
    )
    account_id = cur.fetchone()[0]
    conn.commit()
    conn.close()

    return jsonify({"message": f"{label} linked successfully.", "account_id": str(account_id)})


# --------------- HEALTH ---------------
@app.route('/health', methods=['GET'])
@jwt_required()
def health():
    user_id = get_jwt_identity()
    score = get_credit_score(user_id)
    return jsonify(score)




@app.route('/debug/groq', methods=['GET'])
def debug_groq():
    import requests
    key = os.environ.get("GROQ_API_KEY")
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {key}"}
    payload = {
        "model": "llama-3.3-70b-versatile",
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