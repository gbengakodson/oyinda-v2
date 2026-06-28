# core.py – Oyinda V2 Event Sourcing Core (Final)

import os, hashlib, json, uuid, psycopg2, psycopg2.extras, secrets
from datetime import datetime, timedelta, date

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres.bbjfetsgourtnywqzjqw:2Methylpropane%23@aws-0-eu-west-1.pooler.supabase.com:6543/postgres"
)

def get_conn():
    return psycopg2.connect(DATABASE_URL)

# --------------- Event Hashing ---------------
def compute_event_hash(stream_id, version, payload, previous_hash=None):
    raw = f"{stream_id}|{version}|{json.dumps(payload, sort_keys=True)}|{previous_hash or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()

# --------------- Append Event ---------------
def append_event(user_id: str, stream_id: str, event_type: str, payload: dict, metadata: dict = None) -> dict:
    if metadata is None:
        metadata = {}
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT version, event_hash FROM events WHERE stream_id=%s ORDER BY version DESC LIMIT 1",
            (stream_id,)
        )
        last = cur.fetchone()
        next_version = last[0] + 1 if last else 1
        previous_hash = last[1] if last else None

        new_event_id = uuid.uuid4()
        event_hash = compute_event_hash(stream_id, next_version, payload, previous_hash)

        cur.execute(
            """INSERT INTO events (event_id, user_id, stream_id, event_type, payload, metadata, version, previous_hash, event_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (str(new_event_id), user_id, stream_id, event_type, json.dumps(payload), json.dumps(metadata), next_version, previous_hash, event_hash)
        )
        conn.commit()
        handle_projection(conn, user_id, stream_id, event_type, payload, str(new_event_id))
        return {"event_id": str(new_event_id), "version": next_version}
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

# --------------- Projection Handlers ---------------
def handle_projection(conn, user_id, stream_id, event_type, payload, event_id):
    cur = conn.cursor()
    if event_type == 'ExpenseLogged':
        amount = payload.get('amount', 0)
        currency = payload.get('currency', 'NGN')
        cur.execute(
            "INSERT INTO user_balances (user_id, currency, amount) VALUES (%s, %s, %s) ON CONFLICT (user_id, currency) DO UPDATE SET amount = user_balances.amount - EXCLUDED.amount",
            (user_id, currency, amount)
        )
        cur.execute(
            "INSERT INTO transactions_view (event_id, user_id, date, type, amount, currency, category, description) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (event_id, user_id, payload.get('date', datetime.utcnow().isoformat()), 'expense', amount, currency, payload.get('category', 'other'), payload.get('description', ''))
        )
        update_behavior_log(conn, user_id, payload.get('date', datetime.utcnow().date().isoformat()))
    elif event_type == 'IncomeReceived':
        amount = payload.get('amount', 0)
        currency = payload.get('currency', 'NGN')
        cur.execute(
            "INSERT INTO user_balances (user_id, currency, amount) VALUES (%s, %s, %s) ON CONFLICT (user_id, currency) DO UPDATE SET amount = user_balances.amount + EXCLUDED.amount",
            (user_id, currency, amount)
        )
        cur.execute(
            "INSERT INTO transactions_view (event_id, user_id, date, type, amount, currency, category, description) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (event_id, user_id, payload.get('date', datetime.utcnow().isoformat()), 'income', amount, currency, payload.get('category', 'income'), payload.get('description', ''))
        )
        update_behavior_log(conn, user_id, payload.get('date', datetime.utcnow().date().isoformat()))
    elif event_type == 'GoalSet':
        cur.execute(
            "INSERT INTO goals (goal_id, user_id, stream_id, goal_type, target_amount, currency, deadline, description, saved) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (stream_id, user_id, stream_id, payload.get('goal_type', 'general'), payload.get('target_amount', 0), payload.get('currency', 'NGN'), payload.get('deadline'), payload.get('description', ''), 0)
        )
    elif event_type == 'TransferRequested':
        cur.execute(
            "INSERT INTO transactions_view (event_id, user_id, date, type, amount, currency, category, description, related_stream_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (event_id, user_id, datetime.utcnow().isoformat(), 'transfer_pending', payload.get('amount', 0), payload.get('currency', 'NGN'), 'transfer', payload.get('description', ''), payload.get('destination_account_id'))
        )
    elif event_type == 'TransferExecuted':
        amount = payload.get('amount', 0)
        currency = payload.get('currency', 'NGN')
        cur.execute(
            "UPDATE user_balances SET amount = amount - %s WHERE user_id = %s AND currency = %s",
            (amount, user_id, currency)
        )
        cur.execute(
            "UPDATE transactions_view SET type = 'transfer_executed' WHERE event_id = %s",
            (event_id,)
        )
    elif event_type == 'CryptoOrderExecuted':
        cur.execute(
            "INSERT INTO transactions_view (event_id, user_id, date, type, amount, currency, category, description) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (event_id, user_id, datetime.utcnow().isoformat(), 'crypto_trade', payload.get('quantity', 0), payload.get('symbol', 'USD'), 'trade', payload.get('side', '') + ' ' + payload.get('symbol', ''))
        )
    if event_type in ('ExpenseLogged', 'IncomeReceived', 'TransferExecuted', 'CryptoOrderExecuted'):
        update_credit_score(conn, user_id)
    conn.commit()
    cur.close()

def update_behavior_log(conn, user_id, date_str):
    try:
        if 'T' in date_str:
            dt = datetime.fromisoformat(date_str).date()
        else:
            dt = datetime.strptime(date_str, '%Y-%m-%d').date()
    except:
        dt = date.today()
    cur = conn.cursor()
    cur.execute("""
        SELECT category, SUM(amount) FROM transactions_view
        WHERE user_id=%s AND date::date = %s AND type IN ('expense','transfer_executed','crypto_trade')
        GROUP BY category
    """, (user_id, dt))
    cats = {row[0]: row[1] for row in cur.fetchall()}
    cur.execute("""
        INSERT INTO behavior_log (user_id, date, total_income, total_expense, food, transport, housing, utilities, entertainment, health, clothing, education, other)
        VALUES (%s, %s,
                (SELECT COALESCE(SUM(amount),0) FROM transactions_view WHERE user_id=%s AND date::date=%s AND type='income'),
                (SELECT COALESCE(SUM(amount),0) FROM transactions_view WHERE user_id=%s AND date::date=%s AND type IN ('expense','transfer_executed','crypto_trade')),
                %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, date) DO UPDATE SET
            total_income = EXCLUDED.total_income,
            total_expense = EXCLUDED.total_expense,
            food = EXCLUDED.food,
            transport = EXCLUDED.transport,
            housing = EXCLUDED.housing,
            utilities = EXCLUDED.utilities,
            entertainment = EXCLUDED.entertainment,
            health = EXCLUDED.health,
            clothing = EXCLUDED.clothing,
            education = EXCLUDED.education,
            other = EXCLUDED.other
    """, (user_id, dt, user_id, dt, user_id, dt,
          cats.get('food',0), cats.get('transport',0), cats.get('housing',0),
          cats.get('utilities',0), cats.get('entertainment',0), cats.get('health',0),
          cats.get('clothing',0), cats.get('education',0), cats.get('other',0)))
    conn.commit()
    cur.close()

def update_credit_score(conn, user_id):
    cur = conn.cursor()
    three_months_ago = (datetime.utcnow() - timedelta(days=90)).date()
    cur.execute("SELECT total_income, total_expense FROM behavior_log WHERE user_id=%s AND date >= %s", (user_id, three_months_ago))
    rows = cur.fetchall()
    if not rows:
        score = 20
    else:
        incomes = [r[0] for r in rows if r[0] > 0]
        expenses = [r[1] for r in rows]
        regularity = min(1, len(incomes) / 3)
        total_income = sum(incomes)
        total_expense = sum(expenses)
        savings_rate = (total_income - total_expense) / total_income if total_income > 0 else 0
        score = max(0, min(100, int(savings_rate * 60 + regularity * 40)))
    logo = 'butterfly' if score < 40 else ('eagle' if score >= 70 else 'transition')
    cur.execute("INSERT INTO credit_scores (user_id, score, logo, updated_at) VALUES (%s, %s, %s, now()) ON CONFLICT (user_id) DO UPDATE SET score = EXCLUDED.score, logo = EXCLUDED.logo, updated_at = now()", (user_id, score, logo))
    conn.commit()
    cur.close()

def create_default_connected_account(conn, user_id):
    cur = conn.cursor()
    cur.execute("INSERT INTO connected_accounts (user_id, account_type, provider, label, currency) VALUES (%s, 'bank', 'demo_bank', 'Main NGN Account', 'NGN') ON CONFLICT DO NOTHING", (user_id,))
    conn.commit()
    cur.close()

def get_user_connected_accounts(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, account_type, provider, label, currency FROM connected_accounts WHERE user_id=%s AND is_active=true", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return [{"id": str(r[0]), "type": r[1], "provider": r[2], "label": r[3], "currency": r[4]} for r in rows]

def hash_password(password):
    salt = secrets.token_hex(16)
    return salt + ":" + hashlib.sha256((salt + password).encode()).hexdigest()

def check_password(password, hashed):
    try:
        salt, stored_hash = hashed.split(":", 1)
        return hashlib.sha256((salt + password).encode()).hexdigest() == stored_hash
    except:
        return False

def create_user(name, email, password, account_type="personal", address=""):
    conn = get_conn()
    try:
        cur = conn.cursor()
        user_id = str(uuid.uuid4())
        pwd_hash = hash_password(password)
        cur.execute("INSERT INTO users (id, name, email, password_hash, account_type, address) VALUES (%s,%s,%s,%s,%s,%s)", (user_id, name, email, pwd_hash, account_type, address))
        create_default_connected_account(conn, user_id)
        cur.execute("INSERT INTO credit_scores (user_id, score, logo) VALUES (%s, 20, 'butterfly') ON CONFLICT DO NOTHING", (user_id,))
        cur.execute("INSERT INTO user_balances (user_id, currency, amount) VALUES (%s, 'NGN', 0) ON CONFLICT DO NOTHING", (user_id,))
        conn.commit()
        cur.close()
        return user_id
    except Exception:
        conn.rollback()
        return None
    finally:
        conn.close()

def authenticate_user(email, password):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name, email, password_hash, account_type FROM users WHERE email=%s", (email,))
        row = cur.fetchone()
        if row and check_password(password, row[3]):
            return {"id": str(row[0]), "name": row[1], "email": row[2], "account_type": row[4]}
        return None
    finally:
        conn.close()

def calculate_daily_budget(user_id):
    conn = get_conn()
    cur = conn.cursor()
    three_months_ago = (datetime.utcnow() - timedelta(days=90)).date()
    cur.execute("SELECT AVG(total_income) FROM behavior_log WHERE user_id=%s AND date >= %s", (user_id, three_months_ago))
    avg = cur.fetchone()[0] or 0
    conn.close()
    if avg == 0:
        return None
    needs = avg * 0.5
    wants = avg * 0.3
    return {
        "food": round(needs * 0.3, 2),
        "transport": round(needs * 0.2, 2),
        "housing": round(needs * 0.2, 2),
        "utilities": round(needs * 0.2, 2),
        "other_needs": round(needs * 0.1, 2),
        "entertainment": round(wants * 0.5, 2),
        "clothing": round(wants * 0.3, 2),
        "other_wants": round(wants * 0.2, 2)
    }

def get_credit_score(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT score, logo FROM credit_scores WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {"score": row[0], "logo": row[1]}
    return {"score": 0, "logo": "butterfly"}