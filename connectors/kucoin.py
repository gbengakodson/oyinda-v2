# connectors/kucoin.py
import os, json, hmac, hashlib, time, requests

class KuCoinConnector:
    def __init__(self, api_key, api_secret, passphrase=None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase  # KuCoin requires a passphrase
        self.base_url = "https://api.kucoin.com"

    def _sign(self, method, endpoint, body=''):
        timestamp = str(int(time.time() * 1000))
        str_to_sign = timestamp + method + endpoint + body
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            str_to_sign.encode('utf-8'),
            hashlib.sha256
        ).digest()
        # KuCoin uses base64 of the signature
        import base64
        signature_b64 = base64.b64encode(signature).decode()

        headers = {
            "KC-API-KEY": self.api_key,
            "KC-API-SIGN": signature_b64,
            "KC-API-TIMESTAMP": timestamp,
            "KC-API-PASSPHRASE": hmac.new(
                self.api_secret.encode('utf-8'),
                self.passphrase.encode('utf-8'),
                hashlib.sha256
            ).hexdigest() if self.passphrase else ''
        }
        return headers

    def place_order(self, symbol, side, quantity, order_type='market'):
        """Place a spot order on KuCoin."""
        endpoint = "/api/v1/orders"
        method = "POST"
        body = json.dumps({
            "clientOid": str(int(time.time() * 1000)),
            "side": side.lower(),
            "symbol": symbol.upper(),
            "type": order_type.lower(),
            "size": str(quantity)
        })
        headers = self._sign(method, endpoint, body)
        headers["Content-Type"] = "application/json"

        resp = requests.post(f"{self.base_url}{endpoint}", headers=headers, data=body)
        data = resp.json()
        if data.get("code") != "200000":
            raise Exception(f"KuCoin error: {data.get('msg', 'unknown')}")
        return {"orderId": data["data"]["orderId"]}