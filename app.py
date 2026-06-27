# app.py – Oyinda V2 API (non‑custodial, conversational)

import os
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
from core import *
from groq_parser import parse_intent_groq, classify_query_intent

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
                "date": datetime.utcnow().strftime("%Y-%m-%d"),
                "description": description,
                "source_account_id": source_id,
                "destination_account_id": dest_id
            }
            event = append_event(user_id, user_id, 'TransferRequested', payload)
            source_label = next((a['label'] for a in accounts if a['id'] == source_id), "your account")
            dest_label = next((a['label'] for a in accounts if a['id'] == dest_id), "the destination")
            response_text = f"Okay, I'll send {amount} {currency} from {source_label} to {dest_label}. Please confirm this transfer."
            return jsonify({"message": response_text, "tone": "neutral", "event_id": event['event_id']})

        raw_date = parsed.get("date")
        date = normalize_date(raw_date) if raw_date else datetime.utcnow().strftime("%Y-%m-%d")
        # Non‑transfer events
        if event_type == 'intention':
            payload = {
                "amount": amount,
                "currency": currency,
                "category": category,
                "date": date,  # use normalized date
                "description": description
            }
            event = append_event(user_id, user_id, 'GoalSet', payload)
            response_text = f"Goal set! You want to save {amount} {currency} for {description}. I'll help you stay on track."
            return jsonify({"message": response_text, "tone": "income", "event_id": event['event_id']})

        # income / expense / liability / asset
        if event_type == 'expense':
            final_type = 'ExpenseLogged'
        elif event_type == 'income':
            final_type = 'IncomeReceived'
        elif event_type == 'liability':
            final_type = 'ExpenseLogged'
        elif event_type == 'asset':
            final_type = 'ExpenseLogged'
            category = 'loan_given'  # override category
            parsed['category'] = category
        else:
            final_type = event_type

        payload = {
            "amount": amount,
            "currency": currency,
            "category": category,
            "date": parsed.get("date", datetime.utcnow().strftime("%Y-%m-%d")),
            "description": description
        }

        event = append_event(user_id, user_id, final_type, payload)

        name = get_user_name(user_id)
        if final_type == 'ExpenseLogged':
            if category == 'loan_given':
                response_text = f"Understood, {name}. You lent {amount} {currency}. I'll track this as an asset someone owes you."
                tone = "neutral"
            else:
                response_text = f"Got it, {name}. You spent {amount} {currency} on {category}."

            budget = calculate_daily_budget(user_id)
            if budget:
                total_budget = sum(budget.values())
                daily_limit = total_budget / len(budget) if len(budget) > 0 else 0
                tone = "warning" if amount > daily_limit else "good"
            else:
                tone = "neutral"
        elif final_type == 'IncomeReceived':
            response_text = f"Great, {name}! You received {amount} {currency}. That's a step forward."
            tone = "income"
        else:
            response_text = f"Logged: {text}"
            tone = "neutral"

        return jsonify({"message": response_text, "tone": tone, "event_id": event['event_id']})

    except Exception as e:
        # Return the actual error so we can see it in the frontend
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
    text_lower = text.lower()
    now = datetime.utcnow()

    # Try the AI classifier for structured query understanding
    query_info = classify_query_intent(text)
    if query_info:
        intent = query_info.get('intent')
        params = query_info.get('parameters', {})
        date_param = params.get('date')         # e.g., "this month", "last month"
        category = params.get('category')       # e.g., "food", "transport"

        if intent in ('expense', 'income') and (date_param or category):
            # Build date range
            start, end, label = extract_date_range(date_param or 'all time')
            # Build SQL
            conn = get_conn()
            cur = conn.cursor()
            if intent == 'expense':
                type_filter = "type='expense'"
            else:
                type_filter = "type='income'"
            cat_filter = ""
            query_params = [user_id, start, end]
            if category:
                cat_filter = " AND category = %s"
                query_params.append(category)
            cur.execute(f"SELECT SUM(amount) FROM transactions_view WHERE user_id=%s AND {type_filter} AND date BETWEEN %s AND %s{cat_filter}", query_params)
            total = cur.fetchone()[0] or 0
            conn.close()

            # Human‑friendly reply
            cat_text = f" on {category}" if category else ""
            return jsonify({"answer": f"Total {intent}{cat_text} for {label}: ₦{total:,.2f}", "tone": "neutral"})

    # Fallback: rule‑based checks
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

    # Simple date‑range expense/income (no category)
    if any(w in text_lower for w in ['spent', 'expense', 'spend']):
        start, end, label = extract_date_range(text_lower)
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT SUM(amount) FROM transactions_view WHERE user_id=%s AND type='expense' AND date BETWEEN %s AND %s", (user_id, start, end))
        total = cur.fetchone()[0] or 0
        conn.close()
        return jsonify({"answer": f"Total expenses for {label}: ₦{total:,.2f}", "tone": "neutral"})

    if any(w in text_lower for w in ['made', 'earned', 'income', 'profit']):
        start, end, label = extract_date_range(text_lower)
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT SUM(amount) FROM transactions_view WHERE user_id=%s AND type='income' AND date BETWEEN %s AND %s", (user_id, start, end))
        total = cur.fetchone()[0] or 0
        conn.close()
        return jsonify({"answer": f"Total income for {label}: ₦{total:,.2f}", "tone": "neutral"})

    # Final fallback
    return jsonify({"answer": "I can help with budgets, spending, income, or credit score. Try asking 'how much did I spend on food this month?'", "tone": "neutral"})



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

# --------------- HEALTH SCORE ---------------
@app.route('/health', methods=['GET'])
@jwt_required()
def health():
    user_id = get_jwt_identity()
    score = get_credit_score(user_id)
    return jsonify(score)

# --------------- FRONTEND ---------------
@app.route('/')
def landing():
    return send_from_directory('webapp', 'landing.html')


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
        start = today.strftime("%Y-01")
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