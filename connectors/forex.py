# connectors/forex.py
import requests

class FxProConnector:
    def __init__(self, api_key, api_secret=None):
        self.api_key = api_key
        self.api_secret = api_secret or ''
        self.base_url = "https://api.fxpro.com/v1"

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

    def get_balance(self):
        # FxPro balance endpoint (example)
        resp = requests.get(f"{self.base_url}/accounts/balance", headers=self._headers())
        resp.raise_for_status()
        data = resp.json()
        return data.get("balance", {})

    def get_positions(self):
        resp = requests.get(f"{self.base_url}/positions", headers=self._headers())
        resp.raise_for_status()
        return resp.json().get("positions", [])

    def place_order(self, symbol, side, quantity, order_type="market"):
        payload = {
            "symbol": symbol.upper(),
            "side": side.lower(),
            "volume": quantity,
            "type": order_type
        }
        resp = requests.post(f"{self.base_url}/orders", json=payload, headers=self._headers())
        resp.raise_for_status()
        return resp.json()