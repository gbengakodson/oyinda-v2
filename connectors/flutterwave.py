# connectors/flutterwave.py
import os, requests

FW_SECRET_KEY = os.environ.get("FLUTTERWAVE_SECRET_KEY")
FW_BASE_URL = "https://api.flutterwave.com/v3"

def get_banks(country="NG"):
    """List all supported banks and their codes."""
    resp = requests.get(
        f"{FW_BASE_URL}/banks/{country}",
        headers={"Authorization": f"Bearer {FW_SECRET_KEY}"}
    )
    resp.raise_for_status()
    return resp.json()["data"]

def get_account_details(account_number, bank_code):
    """Resolve a bank account number to an account name."""
    payload = {"account_number": account_number, "account_bank": bank_code}
    resp = requests.post(
        f"{FW_BASE_URL}/accounts/resolve",
        json=payload,
        headers={"Authorization": f"Bearer {FW_SECRET_KEY}"}
    )
    resp.raise_for_status()
    return resp.json()["data"]

def get_transactions(account_id=None, from_date=None, to_date=None):
    """Fetch transactions for a linked account."""
    params = {}
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    resp = requests.get(
        f"{FW_BASE_URL}/transactions",
        headers={"Authorization": f"Bearer {FW_SECRET_KEY}"},
        params=params
    )
    resp.raise_for_status()
    return resp.json()["data"]

def initiate_transfer(amount, currency, bank_code, account_number, narration="Oyinda transfer", reference=None):
    """Transfer money from your Flutterwave balance to any Nigerian bank account."""
    import uuid
    payload = {
        "account_bank": bank_code,
        "account_number": account_number,
        "amount": amount,
        "currency": currency,
        "narration": narration,
        "reference": reference or str(uuid.uuid4()),
        "debit_currency": currency
    }
    resp = requests.post(
        f"{FW_BASE_URL}/transfers",
        json=payload,
        headers={"Authorization": f"Bearer {FW_SECRET_KEY}"}
    )
    resp.raise_for_status()
    return resp.json()