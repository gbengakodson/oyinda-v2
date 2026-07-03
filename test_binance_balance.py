# test_binance_balance.py
import requests, hmac, hashlib, time

# Paste your actual Binance API key and secret here
API_KEY = "KwOJwCGYg6fS9E43NOgyqDHsOcsXffHWtkZ9wfCEfwA5zGPWu1PhiMHw9RJdUxon"
API_SECRET = "uQIJ5je0IjbhM4WCOb4TOHY601OSuFynQRi7YipeOBG8hgkVOco3DCbueIgMtEAZ"

BASE_URL = "https://api.binance.com"
ENDPOINT = "/api/v3/account"

timestamp = int(time.time() * 1000)
query_string = f"timestamp={timestamp}"
signature = hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

url = f"{BASE_URL}{ENDPOINT}?{query_string}&signature={signature}"
headers = {"X-MBX-APIKEY": API_KEY}

resp = requests.get(url, headers=headers)

print("Status Code:", resp.status_code)
print("Response Headers:", dict(resp.headers))
print("Response Body:", resp.text)