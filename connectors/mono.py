# connectors/mono.py
import os, requests
from datetime import datetime, timedelta

MONO_SECRET_KEY = os.environ.get("MONO_SECRET_KEY")
MONO_BASE_URL = "https://api.withmono.com"

def exchange_code(code: str) -> dict:
    """Exchange temporary code from Mono widget for permanent account ID."""
    resp = requests.post(
        f"{MONO_BASE_URL}/account/auth",
        json={"code": code},
        headers={"mono-sec-key": MONO_SECRET_KEY}
    )
    resp.raise_for_status()
    return resp.json()  # {"id": "...", ...}

def get_account_details(account_id: str) -> dict:
    resp = requests.get(
        f"{MONO_BASE_URL}/accounts/{account_id}",
        headers={"mono-sec-key": MONO_SECRET_KEY}
    )
    resp.raise_for_status()
    return resp.json()

def get_transactions(account_id: str, from_date: str = None, to_date: str = None) -> list:
    """
    Fetch paginated transactions. from_date/to_date in YYYY-MM-DD.
    Returns list of transaction dicts.
    """
    params = {"paginate": "true", "limit": 100}
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    all_transactions = []
    url = f"{MONO_BASE_URL}/accounts/{account_id}/transactions"
    while url:
        resp = requests.get(url, headers={"mono-sec-key": MONO_SECRET_KEY}, params=params)
        resp.raise_for_status()
        data = resp.json()
        all_transactions.extend(data.get("data", []))
        # Handle pagination
        meta = data.get("meta")
        if meta and meta.get("next"):
            url = meta["next"]
            params = None  # params only for first request
        else:
            break
    return all_transactions

def initiate_transfer(account_id: str, amount: float, description: str,
                      recipient_bank: str, recipient_account_number: str) -> dict:
    """
    Initiate a bank transfer from the linked Mono account.
    Requires MONO_SECRET_KEY and appropriate permissions.
    """
    resp = requests.post(
        f"{MONO_BASE_URL}/accounts/{account_id}/transfer",
        headers={"mono-sec-key": MONO_SECRET_KEY},
        json={
            "amount": int(amount * 100),  # kobo
            "description": description,
            "recipient_bank": recipient_bank,
            "recipient_account_number": recipient_account_number
        }
    )
    resp.raise_for_status()
    return resp.json()


class MonoReservedAccount:
    """Create virtual NUBAN accounts for users and manage payouts."""
    def __init__(self):
        self.secret_key = MONO_SECRET_KEY
        self.base_url = MONO_BASE_URL

    def _headers(self):
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "mono-sec-key": self.secret_key
        }

    def create_account(self, user_data):
        """
        user_data = {
            "customer": {
                "name": "Mama Obi",
                "email": "mama@example.com",
                "identity": {"type": "bvn", "number": "12345678901"},
                "phone": "+2348033334444"
            },
            "meta": {"user_id": "uuid"}
        }
        Returns the full Mono response.
        """
        url = f"{self.base_url}/v2/accounts"
        resp = requests.post(url, json=user_data, headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    def get_balance(self, account_id):
        url = f"{self.base_url}/v2/accounts/{account_id}/balance"
        resp = requests.get(url, headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    def payout(self, amount, bank_code, account_number, narration):
        """
        Transfer from your Mono pool to an external bank account.
        amount in Naira (int or float).
        """
        url = f"{self.base_url}/v2/payouts"
        data = {
            "amount": amount,
            "bank_code": bank_code,
            "account_number": account_number,
            "narration": narration
        }
        resp = requests.post(url, json=data, headers=self._headers())
        resp.raise_for_status()
        return resp.json()