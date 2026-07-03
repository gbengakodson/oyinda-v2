# connectors/bybit_p2p.py
import requests, hmac, hashlib, time

class BybitP2PConnector:
    BASE_URL = "https://api.bybit.com/v5/p2p"

    def __init__(self, api_key, api_secret):
        self.api_key = api_key
        self.api_secret = api_secret

    def _sign(self, params):
        """Create signature for private endpoints."""
        timestamp = str(int(time.time() * 1000))
        param_str = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
        signature = hmac.new(
            self.api_secret.encode(),
            (timestamp + self.api_key + param_str).encode(),
            hashlib.sha256
        ).hexdigest()
        return timestamp, signature

    def _headers(self, params):
        ts, sig = self._sign(params)
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-SIGN": sig,
            "Content-Type": "application/json"
        }

    def get_best_sell_price(self, token="USDT", currency="NGN"):
        """Fetch the best sell ad price for USDT -> NGN."""
        params = {
            "tokenId": token,
            "currencyId": currency,
            "side": "1",          # 1 = sell (you selling crypto for fiat)
            "page": 1,
            "size": 1,
            "orderType": "price"  # sort by price ascending (best for seller)
        }
        resp = requests.get(
            f"{self.BASE_URL}/ads",
            params=params,
            headers={"X-BAPI-API-KEY": self.api_key}
        )
        resp.raise_for_status()
        data = resp.json()
        ads = data.get("result", {}).get("items", [])
        if not ads:
            return None, None
        best_ad = ads[0]
        return float(best_ad["price"]), best_ad["id"]

    def place_sell_order(self, amount, token="USDT", currency="NGN", ad_id=None):
        """Place a P2P sell order (sell crypto for fiat)."""
        if not ad_id:
            _, ad_id = self.get_best_sell_price(token, currency)
            if not ad_id:
                raise Exception("No available P2P ads to sell USDT for NGN.")

        params = {
            "adId": ad_id,
            "amount": str(amount),
            "tokenId": token,
            "currencyId": currency,
            "side": "1"
        }
        headers = self._headers(params)
        resp = requests.post(
            f"{self.BASE_URL}/order/create",
            json=params,
            headers=headers
        )
        resp.raise_for_status()
        return resp.json()

    def get_best_buy_price(self, token="USDT", currency="NGN"):
        """Fetch the best buy ad price (you buying crypto with fiat)."""
        params = {
            "tokenId": token,
            "currencyId": currency,
            "side": "0",          # 0 = buy
            "page": 1,
            "size": 1,
            "orderType": "price_desc"  # sort by price descending (best for buyer)
        }
        resp = requests.get(
            f"{self.BASE_URL}/ads",
            params=params,
            headers={"X-BAPI-API-KEY": self.api_key}
        )
        resp.raise_for_status()
        data = resp.json()
        ads = data.get("result", {}).get("items", [])
        if not ads:
            return None, None
        best_ad = ads[0]
        return float(best_ad["price"]), best_ad["id"]

    def place_buy_order(self, amount, token="USDT", currency="NGN", ad_id=None):
        """
        Place a P2P buy order (buy crypto with fiat).
        If ad_id is not provided, it finds the best ad automatically.
        Returns a dict with order details and payment information.
        """
        # 1. Get the best ad if not specified
        if not ad_id:
            best_price, ad_id = self.get_best_buy_price(token, currency)
            if not ad_id:
                raise Exception(f"No available P2P ads to buy {token} with {currency}.")

        # 2. Build the request payload
        params = {
            "adId": ad_id,
            "amount": str(amount),
            "tokenId": token,
            "currencyId": currency,
            "side": "0"  # 0 = buy
        }
        headers = self._headers(params)

        # 3. Place the order
        resp = requests.post(
            f"{self.BASE_URL}/order/create",
            json=params,
            headers=headers
        )
        resp.raise_for_status()
        order = resp.json()

        # 4. Extract payment details (the merchant's bank account to pay)
        payment_info = order.get("result", {}).get("payments", [{}])[0]
        result = {
            "order_id": order["result"]["orderId"],
            "amount_crypto": amount,
            "amount_fiat": float(payment_info.get("amount", 0)),
            "bank_name": payment_info.get("bankName", "Unknown"),
            "account_number": payment_info.get("accountNumber", ""),
            "account_name": payment_info.get("accountName", ""),
            "reference": order["result"].get("reference", ""),
            "ad_id": ad_id,
            "bybit_order_url": f"https://www.bybit.com/fiat/trade/otc/orderDetail?orderId={order['result']['orderId']}"
        }
        return result