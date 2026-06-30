# connectors/bybit.py
import hmac, hashlib, time, requests

class BybitConnector:
    def __init__(self, api_key, api_secret):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = "https://api.bybit.com"

    def _sign(self, params):
        """Create signature for Bybit V5 private endpoints."""
        # params must include api_key, timestamp, etc.
        query_string = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
        return hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

    def place_order(self, symbol, side, quantity, order_type='Market'):
        """Place a spot order (Buy/Sell) on Bybit V5."""
        timestamp = int(time.time() * 1000)
        params = {
            "api_key": self.api_key,
            "timestamp": str(timestamp),
            "symbol": symbol.upper(),
            "side": side.capitalize(),
            "orderType": order_type,
            "qty": str(quantity),
            "category": "spot",
        }
        params['sign'] = self._sign(params)

        resp = requests.post(
            f"{self.base_url}/v5/order/create",
            headers={"Content-Type": "application/json"},
            json=params
        )
        data = resp.json()
        if data.get("retCode") != 0:
            raise Exception(f"Bybit error: {data.get('retMsg', 'unknown')}")
        # Return a dict with orderId (our code expects this key)
        return {"orderId": data["result"]["orderId"]}