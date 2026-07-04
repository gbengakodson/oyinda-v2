# connectors/vtpass.py
import os, requests
from datetime import datetime

VTPASS_API_KEY = os.environ.get("VTPASS_API_KEY")
VTPASS_SECRET_KEY = os.environ.get("VTPASS_SECRET_KEY")
VTPASS_BASE_URL = "https://api.vtpass.com/api"

def get_data_plans(network="mtn"):
    """Fetch available data plans for a network."""
    resp = requests.get(
        f"{VTPASS_BASE_URL}/service-variations?serviceID={network}-data",
        auth=(VTPASS_API_KEY, VTPASS_SECRET_KEY)
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("content", {}).get("varations", [])

def buy_data(phone, network, plan_code):
    """Purchase a data bundle. Returns VTpass response."""
    payload = {
        "request_id": f"oyinda_{datetime.utcnow().timestamp()}",
        "serviceID": f"{network}-data",
        "billersCode": phone,
        "variation_code": plan_code,
        "amount": "",   # VTpass fills it from plan
        "phone": phone
    }
    resp = requests.post(
        f"{VTPASS_BASE_URL}/pay",
        json=payload,
        auth=(VTPASS_API_KEY, VTPASS_SECRET_KEY)
    )
    resp.raise_for_status()
    return resp.json()