# connectors/piggyvest.py
# PiggyVest doesn't provide a public API. We'll use a simulated balance for now.
# In production, you could use web scraping (with user consent) or allow manual balance entry.

class PiggyVestConnector:
    def __init__(self, api_key=None):
        self.api_key = api_key   # not used, but kept for interface consistency

    def get_balance(self):
        # Return a mock balance for testing; replace with real logic later
        return {
            "total_savings": 0.0,
            "interest_earned": 0.0
        }