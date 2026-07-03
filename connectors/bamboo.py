# connectors/bamboo.py
import requests

class BambooConnector:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://api.bambooinvest.com/v1"

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

    def get_balance(self):
        resp = requests.get(f"{self.base_url}/account", headers=self._headers())
        resp.raise_for_status()
        data = resp.json()
        return data.get("balance", {})

    def get_positions(self):
        resp = requests.get(f"{self.base_url}/positions", headers=self._headers())
        resp.raise_for_status()
        return resp.json().get("positions", [])

    def get_transactions(self, from_date=None, to_date=None):
        params = {}
        if from_date: params["from"] = from_date
        if to_date: params["to"] = to_date
        resp = requests.get(f"{self.base_url}/transactions", headers=self._headers(), params=params)
        resp.raise_for_status()
        return resp.json().get("transactions", [])

    def place_order(self, symbol, side, quantity, order_type="market"):
        payload = {
            "symbol": symbol.upper(),
            "side": side.lower(),
            "quantity": quantity,
            "type": order_type
        }
        resp = requests.post(f"{self.base_url}/orders", json=payload, headers=self._headers())
        resp.raise_for_status()
        return resp.json()