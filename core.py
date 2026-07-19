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
        # Get last version and previous hash
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

        # Run projections
        handle_projection(conn, user_id, stream_id, event_type, payload, str(new_event_id))

        # ----- NEW: Record daily activity & data reward -----
        try:
            cur.execute(
                "INSERT INTO daily_activity_log (user_id, date) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (user_id, datetime.utcnow().date())
            )
            if cur.rowcount == 1:   # A new day was inserted
                # Give 33.33 MB data credit (1 GB / 30 days)
                cur.execute(
                    "UPDATE users SET data_balance_mb = COALESCE(data_balance_mb, 0) + 33.33 WHERE id = %s",
                    (user_id,)
                )
            conn.commit()
        except Exception:
            pass  # Never let activity logging break the main event flow
        # ------------------------------------------------------

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
    today = datetime.utcnow().date()

    # ---------- 1. Payment History (35%) ----------
    # For now we treat all loans as “not yet repaid” — no late marks.
    # If LoanRepaid events exist, the user gets full points.
    cur.execute("SELECT COUNT(*) FROM events WHERE user_id=%s AND event_type='LoanRepaid'", (user_id,))
    repaid_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM events WHERE user_id=%s AND event_type='ExpenseLogged' AND payload->>'category' = 'loan'", (user_id,))
    loan_count = cur.fetchone()[0]
    if loan_count == 0:
        payment_score = 100   # no loans, full points
    else:
        payment_score = 100 if repaid_count >= loan_count else max(30, 100 - (loan_count - repaid_count) * 15)

    # ---------- 2. Credit Utilisation (30%) ----------
    cur.execute("SELECT COALESCE(SUM(amount),0) FROM transactions_view WHERE user_id=%s AND type='income' AND date >= %s", (user_id, today - timedelta(days=365)))
    annual_income = cur.fetchone()[0]
    cur.execute("SELECT COALESCE(SUM(amount),0) FROM transactions_view WHERE user_id=%s AND type='expense' AND category='loan'", (user_id,))
    total_loans = cur.fetchone()[0]
    util_ratio = (total_loans / annual_income) if annual_income > 0 else 1.0
    if util_ratio <= 0.30:
        util_score = 100
    elif util_ratio >= 1.0:
        util_score = 0
    else:
        util_score = int(100 - (util_ratio - 0.30) * 150)

    # ---------- 3. Length of Credit History (15%) ----------
    cur.execute("SELECT MIN(created_at) FROM events WHERE user_id=%s", (user_id,))
    first_event = cur.fetchone()[0]
    if first_event:
        months = max(1, (today - first_event.date()).days // 30)
    else:
        months = 0
    if months > 24:
        length_score = 100
    elif months > 12:
        length_score = 80
    elif months > 6:
        length_score = 60
    elif months > 3:
        length_score = 40
    else:
        length_score = 20

    # ---------- 4. Credit Mix (10%) ----------
    cur.execute("SELECT COUNT(DISTINCT CASE WHEN event_type IN ('ExpenseLogged','IncomeReceived','LoanRepaid','InvestmentMade','GoalSet') THEN event_type ELSE NULL END) FROM events WHERE user_id=%s", (user_id,))
    distinct_types = cur.fetchone()[0]
    mix_score = min(100, distinct_types * 25)

    # ---------- 5. New Credit (10%) ----------
    six_months_ago = today - timedelta(days=180)
    cur.execute("SELECT COUNT(*) FROM events WHERE user_id=%s AND event_type='ExpenseLogged' AND payload->>'category' = 'loan' AND created_at >= %s", (user_id, six_months_ago))
    new_loans = cur.fetchone()[0]
    if new_loans == 0:
        new_credit_score = 100
    elif new_loans <= 2:
        new_credit_score = 70
    elif new_loans <= 4:
        new_credit_score = 40
    else:
        new_credit_score = 10

    # ---------- Composite ----------
    raw = (payment_score * 0.35) + (util_score * 0.30) + (length_score * 0.15) + (mix_score * 0.10) + (new_credit_score * 0.10)
    fico = int(300 + (raw / 100) * 550)

    # Logo
    if fico < 580:
        logo = 'butterfly'
    elif fico < 740:
        logo = 'transition'
    else:
        logo = 'eagle'

    # Store breakdown
    breakdown = {
        "fico": fico,
        "logo": logo,
        "pillars": {
            "payment_history": {"score": payment_score, "weight": "35%", "note": "Based on your loan repayments"},
            "credit_utilization": {"score": util_score, "weight": "30%", "note": "How much debt vs income"},
            "credit_age": {"score": length_score, "weight": "15%", "note": "How long you've tracked money with Oyinda"},
            "credit_mix": {"score": mix_score, "weight": "10%", "note": "Different types of transactions"},
            "new_credit": {"score": new_credit_score, "weight": "10%", "note": "Recent new loans"}
        }
    }

    cur.execute("INSERT INTO credit_scores (user_id, score, logo, updated_at) VALUES (%s, %s, %s, now()) ON CONFLICT (user_id) DO UPDATE SET score=EXCLUDED.score, logo=EXCLUDED.logo, updated_at=now()", (user_id, fico, logo))
    conn.commit()
    cur.close()
    return breakdown   # so it can be used by the query handler



def get_credit_score(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT score, logo FROM credit_scores WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {"score": row[0], "logo": row[1]}
    return {"score": 0, "logo": "butterfly"}


def get_user_facts(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT facts FROM users WHERE id=%s", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row and row[0]:
        return row[0]   # a dict
    return {}

def store_user_fact(user_id, fact_key, fact_value):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET facts = COALESCE(facts, '{}'::jsonb) || %s WHERE id = %s",
        (json.dumps({fact_key: fact_value}), user_id)
    )
    conn.commit()
    conn.close()


def calculate_net_worth(user_id):
    accounts = get_user_connected_accounts(user_id)
    assets = []
    total_assets_ngn = 0.0
    rates = {
        'NGN':1.0, 'USD':1500.0, 'ETH':2000000.0, 'BNB':250000.0,
        'USDT':1500.0, 'USDC':1500.0, 'BUSD':1500.0, 'DAI':1500.0
    }

    # Deduplicate wallets by address
    seen_addresses = set()
    unique_accounts = []
    for acc in accounts:
        addr = acc.get('wallet_address')
        if addr:
            if addr in seen_addresses:
                continue
            seen_addresses.add(addr)
        unique_accounts.append(acc)

    for acc in unique_accounts:
        try:
            from connectors.balances import get_account_balance
            balance_str = get_account_balance(acc)
            import re

            # If the balance_str already starts with the label, we'll use it as is
            # and not prepend the label again.
            # We'll still parse for native/token amounts.

            native_parsed = False
            token_parsed = False

            # Parse native balance (first line)
            native_match = re.search(r'([\d,]+\.?\d*)\s*(BNB|ETH|MATIC|TRX|SOL)', balance_str)
            if native_match:
                amount = float(native_match.group(1).replace(',', ''))
                currency = native_match.group(2)
                rate = rates.get(currency, 1.0)
                ngn_value = amount * rate
                total_assets_ngn += ngn_value
                assets.append(f"{acc['label']}: {amount:,.4f} {currency} (≈ ₦{ngn_value:,.2f})")
                native_parsed = True

            # Parse token balances
            token_line = re.search(r'Tokens:\s*(.*)', balance_str)
            if token_line:
                token_string = token_line.group(1)
                tokens = re.findall(r'(\w+):\s*([\d,]+\.?\d*)', token_string)
                for token_name, amount_str in tokens:
                    amount = float(amount_str.replace(',', ''))
                    currency = token_name.upper()
                    rate = rates.get(currency, 1.0)
                    ngn_value = amount * rate
                    total_assets_ngn += ngn_value
                    assets.append(f"  └ {token_name}: {amount:,.4f} (≈ ₦{ngn_value:,.2f})")
                token_parsed = True

            # If we parsed nothing, show the raw string (but don't double-label)
            if not native_parsed and not token_parsed:
                # balance_str likely already contains the label; just use it
                assets.append(balance_str)
        except Exception as e:
            assets.append(f"{acc['label']}: error ({str(e)})")


    # Add in‑app wallet balance
    conn_wallet = get_conn()
    cur_wallet = conn_wallet.cursor()
    cur_wallet.execute("SELECT balance FROM user_wallets WHERE user_id = %s", (user_id,))
    wallet_row = cur_wallet.fetchone()
    conn_wallet.close()
    if wallet_row and wallet_row[0]:
        wallet_balance = float(wallet_row[0])
        total_assets_ngn += wallet_balance
        assets.append(f"Oyinda Wallet: ₦{wallet_balance:,.2f}")

    # Liabilities
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT SUM(amount) FROM transactions_view WHERE user_id=%s AND type='expense' AND category='loan'",
                (user_id,))
    total_loans = cur.fetchone()[0] or 0
    conn.close()

    net_worth = total_assets_ngn - total_loans
    result = f"**Your Net Worth**\n\nAssets:\n"
    for a in assets:
        result += f"• {a}\n"
    result += f"\nTotal Assets (NGN): ₦{total_assets_ngn:,.2f}\n"
    result += f"Total Liabilities (Loans): ₦{total_loans:,.2f}\n"
    result += f"**Net Worth: ₦{net_worth:,.2f}**"
    return result


def calculate_all_taxes(user_id):
    """Calculate all applicable Nigerian taxes based on logged income."""
    conn = get_conn()
    cur = conn.cursor()
    today = datetime.utcnow().date()
    year_start = today.replace(month=1, day=1).strftime('%Y-%m-%d')
    today_str = today.strftime('%Y-%m-%d')

    # ----- Get total income by category for this year -----
    cur.execute("""
        SELECT category, SUM(amount) FROM transactions_view
        WHERE user_id=%s AND type='income' AND date BETWEEN %s AND %s
        GROUP BY category
    """, (user_id, year_start, today_str))
    rows = cur.fetchall()
    conn.close()

    income_by_cat = {row[0]: row[1] for row in rows}
    total_income = sum(income_by_cat.values())

    # Map categories to tax types
    business_cats = ['income', 'business', 'sales', 'freelance', 'gig', 'side hustle']
    salary_cats = ['salary', 'wages', 'stipend', 'allowance']
    investment_cats = ['investment', 'dividend', 'interest', 'capital gains']
    rental_cats = ['rental income', 'rent income', 'property']

    business_income = sum(income_by_cat.get(cat, 0) for cat in business_cats)
    salary_income = sum(income_by_cat.get(cat, 0) for cat in salary_cats)
    investment_income = sum(income_by_cat.get(cat, 0) for cat in investment_cats)
    rental_income = sum(income_by_cat.get(cat, 0) for cat in rental_cats)

    taxes = []

    # 1. Presumptive Tax (for micro‑businesses)
    # Nigerian presumptive tax is typically a flat rate based on turnover.
    # Example: 0.5% of turnover, minimum ₦5,000, maximum ₦50,000.
    if business_income > 0:
        presumptive_rate = 0.005  # 0.5%
        presumptive_tax = max(5000, min(50000, business_income * presumptive_rate))
        taxes.append({
            "tax_type": "Presumptive Tax (Business)",
            "taxable_income": round(business_income, 2),
            "tax_amount": round(presumptive_tax, 2),
            "brackets": f"0.5% of ₦{business_income:,.2f} turnover"
        })

    # 2. Personal Income Tax (PAYE)
    if salary_income > 0:
        # Simplified PAYE brackets
        if salary_income <= 300000:
            paye = 0
        elif salary_income <= 600000:
            paye = (salary_income - 300000) * 0.07
        elif salary_income <= 12000000:
            paye = 300000 * 0.07 + (salary_income - 600000) * 0.15
        elif salary_income <= 30000000:
            paye = 300000 * 0.07 + 6000000 * 0.15 + (salary_income - 12000000) * 0.25
        else:
            paye = 300000 * 0.07 + 6000000 * 0.15 + 18000000 * 0.25 + (salary_income - 30000000) * 0.30
        taxes.append({
            "tax_type": "PAYE (Salary)",
            "taxable_income": round(salary_income, 2),
            "tax_amount": round(paye, 2),
            "brackets": "Nigerian PAYE brackets"
        })

    # 3. Withholding Tax on Investment Income
    if investment_income > 0:
        withholding_rate = 0.10
        wht = investment_income * withholding_rate
        taxes.append({
            "tax_type": "Withholding Tax (Investments)",
            "taxable_income": round(investment_income, 2),
            "tax_amount": round(wht, 2),
            "brackets": f"10% of ₦{investment_income:,.2f}"
        })

    # 4. Withholding Tax on Rental Income
    if rental_income > 0:
        rent_wht_rate = 0.10
        rent_wht = rental_income * rent_wht_rate
        taxes.append({
            "tax_type": "Withholding Tax (Rent)",
            "taxable_income": round(rental_income, 2),
            "tax_amount": round(rent_wht, 2),
            "brackets": f"10% of ₦{rental_income:,.2f}"
        })

    total_tax = sum(t['tax_amount'] for t in taxes)

    return {
        "taxes": taxes,
        "total_tax": total_tax,
        "total_income": round(total_income, 2),
        "year": today.year,
        "generated_at": datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    }



def create_default_connected_account(conn, user_id):
    cur = conn.cursor()
    cur.execute("INSERT INTO connected_accounts (user_id, account_type, provider, label, currency) VALUES (%s, 'bank', 'demo_bank', 'Main NGN Account', 'NGN') ON CONFLICT DO NOTHING", (user_id,))
    conn.commit()
    cur.close()

def get_user_connected_accounts(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, account_type, provider, label, currency, wallet_address, network, api_key_encrypted, api_secret_encrypted FROM connected_accounts WHERE user_id=%s AND is_active=true",
        (user_id,)
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id": str(r[0]),
            "type": r[1],
            "provider": r[2],
            "label": r[3],
            "currency": r[4],
            "wallet_address": r[5],
            "network": r[6],
            "api_key_encrypted": r[7],
            "api_secret_encrypted": r[8],
        }
        for r in rows
    ]

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
    except Exception as e:
        conn.rollback()
        return f"error: {str(e)}"
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


def authenticate_user_by_email_or_username(email_or_username, password):
    conn = get_conn()
    try:
        cur = conn.cursor()
        # Try email first
        cur.execute("SELECT id, name, email, password_hash, account_type FROM users WHERE email=%s", (email_or_username,))
        row = cur.fetchone()
        if not row:
            # Try username
            cur.execute("SELECT id, name, email, password_hash, account_type FROM users WHERE username=%s", (email_or_username,))
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

def update_credit_score(conn, user_id):
    cur = conn.cursor()

    # 1. Cash‑flow metrics (last 90 days)
    three_months_ago = (datetime.utcnow() - timedelta(days=90)).date()
    cur.execute("SELECT total_income, total_expense FROM behavior_log WHERE user_id=%s AND date >= %s", (user_id, three_months_ago))
    rows = cur.fetchall()
    income_sum = sum(r[0] for r in rows)
    expense_sum = sum(r[1] for r in rows)
    savings_rate = (income_sum - expense_sum) / income_sum if income_sum > 0 else 0

    # 2. Net worth (assets – liabilities)
    try:
        net_worth_str = calculate_net_worth(user_id)
        # Extract the final net worth number from the string
        import re
        match = re.search(r'Net Worth: ₦([\d,]+\.?\d*)', net_worth_str)
        if match:
            net_worth = float(match.group(1).replace(',', ''))
        else:
            net_worth = 0
    except:
        net_worth = 0

    # 3. Composite score (0‑100)
    # – Savings rate (0‑1) → 50 points max
    # – Positive net worth → 30 points
    # – Regular income (≥2 months) → 20 points
    income_months = len(set(r[0] for r in rows if r[0] > 0))   # distinct months with income
    regularity = min(1, income_months / 3)

    score = int(
        savings_rate * 50 +
        (30 if net_worth > 0 else max(0, 30 + net_worth / 10000)) +   # scale down if negative
        regularity * 20
    )
    score = max(0, min(100, score))

    # Logo: <40 butterfly, 40‑69 transition, ≥70 eagle
    logo = 'butterfly' if score < 40 else ('eagle' if score >= 70 else 'transition')

    cur.execute("INSERT INTO credit_scores (user_id, score, logo, updated_at) VALUES (%s, %s, %s, now()) ON CONFLICT (user_id) DO UPDATE SET score = EXCLUDED.score, logo = EXCLUDED.logo, updated_at = now()",
                (user_id, score, logo))
    conn.commit()
    cur.close()