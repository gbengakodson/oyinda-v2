# connectors/exchange_factory.py
from utils.crypto import decrypt
from connectors.exchange import BinanceConnector

def get_exchange_connector(account):
    provider = account.get('provider', '').lower()
    api_key = decrypt(account['api_key_encrypted'])
    api_secret = decrypt(account['api_secret_encrypted'])

    if provider == 'binance':
        return BinanceConnector(api_key, api_secret)
    elif provider == 'bybit':
        from connectors.bybit import BybitConnector
        return BybitConnector(api_key, api_secret)
    elif provider == 'kucoin':
        # Placeholder for future
        raise NotImplementedError("KuCoin connector coming soon.")
    elif provider == 'coinbase':
        raise NotImplementedError("Coinbase connector coming soon.")
    else:
        raise ValueError(f"Unsupported exchange: {provider}")
