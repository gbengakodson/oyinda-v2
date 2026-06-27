# connectors/exchange.py
import os, hmac, hashlib, time, requests
from urllib.parse import urlencode
from utils.crypto import decrypt, encrypt

class BinanceConnector:
    def __init__(self, api_key, api_secret):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = "https://api.binance.com"

    def _sign(self, params):
        query_string = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature

    def _request(self, method, path, params=None, signed=False):
        if params is None:
            params = {}
        if signed:
            params['timestamp'] = int(time.time() * 1000)
            params['signature'] = self._sign(params)
            headers = {'X-MBX-APIKEY': self.api_key}
        else:
            headers = {}
        url = self.base_url + path
        resp = requests.request(method, url, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def get_balances(self):
        data = self._request('GET', '/api/v3/account', signed=True)
        balances = {}
        for b in data.get('balances', []):
            if float(b['free']) > 0 or float(b['locked']) > 0:
                balances[b['asset']] = {'free': float(b['free']), 'locked': float(b['locked'])}
        return balances

    def get_deposit_history(self, asset=None):
        params = {}
        if asset: params['asset'] = asset
        return self._request('GET', '/sapi/v1/capital/deposit/hisrec', params=params, signed=True)

    def get_withdraw_history(self, asset=None):
        params = {}
        if asset: params['asset'] = asset
        return self._request('GET', '/sapi/v1/capital/withdraw/history', params=params, signed=True)

    def place_order(self, symbol, side, quantity, price=None, order_type='MARKET'):
        params = {
            'symbol': symbol.upper(),
            'side': side.upper(),  # BUY or SELL
            'type': order_type.upper(),
            'quantity': quantity
        }
        if order_type.upper() == 'LIMIT':
            params['price'] = price
            params['timeInForce'] = 'GTC'
        return self._request('POST', '/api/v3/order', params=params, signed=True)

    def withdraw(self, asset, address, amount, network=None):
        params = {
            'asset': asset.upper(),
            'address': address,
            'amount': amount
        }
        if network: params['network'] = network
        return self._request('POST', '/sapi/v1/capital/withdraw/apply', params=params, signed=True)

def get_exchange_connector(user_id, account_id):
    """Retrieve and decrypt API keys for a given connected exchange account."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT provider, api_key_encrypted, api_secret_encrypted FROM connected_accounts WHERE id=%s AND user_id=%s", (account_id, user_id))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    provider, enc_key, enc_secret = row
    if provider != 'binance':
        return None
    return BinanceConnector(decrypt(enc_key), decrypt(enc_secret))