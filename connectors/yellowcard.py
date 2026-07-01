# connectors/yellowcard.py
import requests

class YellowCardConnector:
    BASE_URL = "https://api.yellowcard.io/v1"

    def __init__(self, api_key, api_secret):
        self.api_key = api_key
        self.api_secret = api_secret

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-API-Key": self.api_secret,
        }

    def get_rate(self, from_currency, to_currency):
        """Get current exchange rate (e.g., USDT → NGN)."""
        resp = requests.get(
            f"{self.BASE_URL}/rates/{from_currency}/{to_currency}",
            headers=self._headers()
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", {}).get("rate")

    def sell_crypto(self, amount, currency="USDT"):
        """Sell crypto and receive fiat to the linked bank account."""
        payload = {
            "amount": amount,
            "currency": currency,
            "action": "sell"
        }
        resp = requests.post(
            f"{self.BASE_URL}/orders",
            json=payload,
            headers=self._headers()
        )
        resp.raise_for_status()
        return resp.json()

    def buy_crypto(self, amount, currency="NGN"):
        """Buy crypto using fiat from the linked bank account."""
        payload = {
            "amount": amount,
            "currency": currency,
            "action": "buy"
        }
        resp = requests.post(
            f"{self.BASE_URL}/orders",
            json=payload,
            headers=self._headers()
        )
        resp.raise_for_status()
        return resp.json()