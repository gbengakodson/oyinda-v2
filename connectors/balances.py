# connectors/balances.py
import os, requests
from utils.crypto import decrypt

def get_account_balance(account: dict) -> str:
    try:
        acc_type = account['type']
        if acc_type == 'bank':
            return _get_bank_balance(account)
        elif acc_type == 'exchange':
            return _get_exchange_balance(account)
        elif acc_type == 'wallet':
            return _get_wallet_balance(account)
        else:
            return f"{account['label']}: balance unavailable for this account type."
    except Exception as e:
        print("BALANCE_FETCH_ERROR:", account.get('label'), str(e))
        import traceback
        traceback.print_exc()
        return f"{account['label']}: balance unavailable"

def _get_bank_balance(account):
    # Use Mono API to get real-time balance
    # Requires MONO_SECRET_KEY and the mono_account_id stored in connected_accounts
    # For now, we return a placeholder – implement later when Mono keys are ready
    return f"{account['label']}: bank balance fetching coming soon."

def _get_exchange_balance(account):
    if account['provider'] == 'binance':
        from connectors.exchange import BinanceConnector
        api_key = decrypt(account['api_key_encrypted'])
        api_secret = decrypt(account['api_secret_encrypted'])
        connector = BinanceConnector(api_key, api_secret)
        balances = connector.get_balances()
        # Format a summary
        lines = [f"{asset}: {data['free']}" for asset, data in balances.items() if data['free'] > 0]
        if not lines:
            return f"{account['label']}: no assets found."
        return f"{account['label']} holdings:\n" + "\n".join(lines)
    else:
        return f"{account['label']}: exchange not supported yet."

def _get_wallet_balance(account):
    address = account['wallet_address']
    network = account['network'].lower()
    if network in ('ethereum', 'bsc', 'bsc testnet'):
        # Use Etherscan / BscScan
        return _get_evm_balance(address, network)
    elif network == 'tron':
        return _get_tron_balance(address)
    else:
        return f"{account['label']}: unsupported network for balance lookup."


def _get_evm_balance(address, network):
    # Use the new V2 endpoints for BscScan / Etherscan
    if network == 'bsc':
        api_key = os.environ.get("BSCSCAN_API_KEY", "")
        base_url = "https://api.bscscan.com/v2/api"
        chain = "bsc"
    else:  # ethereum or testnets
        api_key = os.environ.get("ETHERSCAN_API_KEY", "")
        base_url = "https://api.etherscan.io/v2/api"
        chain = "eth"

    params = {
        "chain": chain,
        "module": "account",
        "action": "balance",
        "address": address,
        "tag": "latest"
    }
    if api_key:
        params["apikey"] = api_key

    try:
        resp = requests.get(base_url, params=params, timeout=10)
        data = resp.json()
        print("EVMBALANCE_DEBUG:", network, address, data)   # debug
        if data.get("status") == "1":
            balance_wei = int(data["result"])
            balance = balance_wei / 1e18
            return f"{network.upper()} Wallet ({address[:6]}...): {balance:.4f} {network.upper().split(' ')[0]}"
        else:
            return f"{network.upper()} Wallet ({address[:6]}...): {data.get('message', 'NOTOK')} (Raw: {data})"
    except Exception as e:
        print("EVMBALANCE_ERROR:", str(e))
        return f"{network.upper()} Wallet ({address[:6]}...): error fetching balance ({str(e)})"


def _get_tron_balance(address):
    # TronGrid API
    api_key = os.environ.get("TRONGRID_API_KEY", "")
    url = f"https://api.trongrid.io/v1/accounts/{address}"
    headers = {"TRON-PRO-API-KEY": api_key} if api_key else {}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        if data.get("data"):
            balance_sun = data["data"][0].get("balance", 0)
            balance_trx = balance_sun / 1e6
            return f"{account['label']}: {balance_trx:.4f} TRX"
        else:
            return f"{account['label']}: address not found on Tron."
    except Exception as e:
        return f"{account['label']}: error fetching TRX balance ({str(e)})"
