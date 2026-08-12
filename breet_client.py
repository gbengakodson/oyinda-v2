import os
import uuid
import time
import random

# ------------------------------
# MOCK MODE
# Set BREET_MOCK=true in your environment to use this mock client
# ------------------------------

class BreetClient:
    def __init__(self):
        self.mock_mode = os.getenv("BREET_MOCK", "true").lower() == "true"
        self.api_key = os.getenv("BREET_API_KEY")
        self.secret_key = os.getenv("BREET_SECRET_KEY")
        if not self.mock_mode and (not self.api_key or not self.secret_key):
            raise Exception("Breet API keys missing while mock mode is off")

    def create_subaccount(self, user_id, bank_name, account_number, account_name):
        """
        Create a dedicated Breet deposit address for the user.
        In mock mode, generate a fake address and subaccount ID.
        """
        if self.mock_mode:
            # Simulate network delay
            time.sleep(0.5)
            fake_subaccount_id = f"mock_sub_{user_id}_{uuid.uuid4().hex[:8]}"
            fake_address = f"0x{uuid.uuid4().hex[:40]}"  # BEP20-like address
            return {
                "subaccount_id": fake_subaccount_id,
                "address": fake_address,
                "network": "BEP20",
                "bank_name": bank_name,
                "account_number": account_number,
                "account_name": account_name
            }
        else:
            # TODO: Real Breet API call
            # import requests
            # resp = requests.post(
            #     "https://api.breet.app/v1/business/sub-account",
            #     headers={"Authorization": f"Bearer {self.secret_key}"},
            #     json={...}
            # )
            # return resp.json()
            raise NotImplementedError("Real Breet API not implemented yet")

    def get_rate(self, from_currency="USDT", to_currency="NGN"):
        """
        Return the current conversion rate for USDT to NGN.
        Mock mode returns a random but realistic rate.
        """
        if self.mock_mode:
            # NGN per USDT varies between 1450 and 1550
            rate = random.randint(1450, 1550)
            return rate
        else:
            # TODO: Real Breet API rate fetch
            raise NotImplementedError("Real Breet API not implemented yet")

    def get_subaccount_address(self, subaccount_id):
        """
        Return the deposit address for an existing Breet subaccount.
        In mock mode, just return the subaccount_id (address stored locally).
        """
        if self.mock_mode:
            # In mock mode, we don't actually have a Breet subaccount.
            # We'll just return a fake address.
            return f"0x{uuid.uuid4().hex[:40]}"
        else:
            raise NotImplementedError("Real Breet API not implemented yet")