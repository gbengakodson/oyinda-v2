# connectors/okra.py
import os, requests

OKRA_SECRET_KEY = os.environ.get("OKRA_SECRET_KEY")
OKRA_BASE_URL = "https://api.okra.ng/v2"

def exchange_code(code: str) -> dict:
    """Exchange the temporary code from the Okra widget for a permanent customer + account ID."""
    resp = requests.post(
        f"{OKRA_BASE_URL}/products/auths",
        json={"code": code},
        headers={"Authorization": f"Bearer {OKRA_SECRET_KEY}"}
    )
    resp.raise_for_status()
    return resp.json()

def get_account_details(account_id: str) -> dict:
    """Fetch details of a single linked account."""
    resp = requests.get(
        f"{OKRA_BASE_URL}/accounts/{account_id}",
        headers={"Authorization": f"Bearer {OKRA_SECRET_KEY}"}
    )
    resp.raise_for_status()
    return resp.json()

def get_transactions(account_id: str, from_date: str = None, to_date: str = None) -> list:
    """Fetch transactions for a linked account. Dates in YYYY-MM-DD."""
    params = {"limit": 100}
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    all_tx = []
    url = f"{OKRA_BASE_URL}/transactions"
    while url:
        resp = requests.get(url, headers={"Authorization": f"Bearer {OKRA_SECRET_KEY}"}, params=params)
        resp.raise_for_status()
        data = resp.json()
        all_tx.extend(data.get("data", []))
        # Okra paginates via a "next" link if available
        url = data.get("meta", {}).get("next") if data.get("meta") else None
        params = None  # only for the first request
    return all_tx

def initiate_transfer(account_id: str, amount: float, recipient_bank_code: str,
                      recipient_account_number: str, narration: str = "") -> dict:
    """Transfer money from the linked account to any Nigerian bank account."""
    payload = {
        "account_id": account_id,
        "amount": int(amount * 100),  # kobo
        "bank_code": recipient_bank_code,
        "account_number": recipient_account_number,
        "narration": narration
    }
    resp = requests.post(
        f"{OKRA_BASE_URL}/transfers",
        json=payload,
        headers={"Authorization": f"Bearer {OKRA_SECRET_KEY}"}
    )
    resp.raise_for_status()
    return resp.json()