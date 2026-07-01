# connectors/payment_gateway.py
import os, requests, uuid
from utils.crypto import encrypt, decrypt

def link_flutterwave(user_id, api_key, api_secret=None):
    # Test the key by fetching balances
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = requests.get("https://api.flutterwave.com/v3/balances", headers=headers)
    if resp.status_code != 200:
        raise Exception("Invalid Flutterwave API key")
    enc_key = encrypt(api_key)
    # Store in connected_accounts
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO connected_accounts (user_id, account_type, provider, label, currency, api_key_encrypted) VALUES (%s, 'payment', 'flutterwave', 'Flutterwave Account', 'NGN', %s) RETURNING id",
        (user_id, enc_key)
    )
    account_id = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return {"message": "Flutterwave account linked.", "account_id": str(account_id)}

def link_paystack(user_id, api_key):
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = requests.get("https://api.paystack.co/transaction", headers=headers)
    if resp.status_code != 200:
        raise Exception("Invalid Paystack API key")
    enc_key = encrypt(api_key)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO connected_accounts (user_id, account_type, provider, label, currency, api_key_encrypted) VALUES (%s, 'payment', 'paystack', 'Paystack Account', 'NGN', %s) RETURNING id",
        (user_id, enc_key)
    )
    account_id = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return {"message": "Paystack account linked.", "account_id": str(account_id)}