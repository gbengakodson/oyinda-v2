# connectors/vtpass.py
import os, requests
from datetime import datetime

VTPASS_API_KEY = os.environ.get("VTPASS_API_KEY")
VTPASS_SECRET_KEY = os.environ.get("VTPASS_SECRET_KEY")
# Use sandbox for testing; switch to live later
VTPASS_BASE_URL = "https://sandbox.vtpass.com/api"

def get_data_plans(network="mtn"):
    """Fetch available data plans for a network."""
    service_id = f"{network}-data"
    resp = requests.get(
        f"{VTPASS_BASE_URL}/service-variations",
        params={"serviceID": service_id},
        auth=(VTPASS_API_KEY, VTPASS_SECRET_KEY)
    )
    resp.raise_for_status()
    data = resp.json()
    # The response has both "variations" and a misspelled "varations"; we use "variations"
    return data.get("content", {}).get("variations", [])

def buy_data(phone, network, plan_code, amount=None):
    """Purchase a data bundle. Returns VTpass response."""
    payload = {
        "request_id": f"oyinda_{datetime.utcnow().timestamp()}",
        "serviceID": f"{network}-data",
        "billersCode": phone,
        "variation_code": plan_code,
        "phone": phone,
    }
    if amount:
        payload["amount"] = str(amount)
    resp = requests.post(
        f"{VTPASS_BASE_URL}/pay",
        json=payload,
        auth=(VTPASS_API_KEY, VTPASS_SECRET_KEY)
    )
    resp.raise_for_status()
    return resp.json()