# connectors/monica.py
import requests

class MonicaConnector:
    BASE_URL = "https://api.monica.im/v1"

    def __init__(self, api_key):
        self.api_key = api_key

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

    def get_deposit_address(self, network="TRC20"):
        """Get your Monica deposit address for USDT (default TRC20)."""
        resp = requests.get(
            f"{self.BASE_URL}/wallet/address",
            params={"network": network, "currency": "USDT"},
            headers=self._headers()
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("address")

    def sell_crypto(self, amount, currency="USDT"):
        """Initiate an automatic sell (USDT → NGN)."""
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