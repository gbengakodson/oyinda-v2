# connectors/balances.py
import os, requests
from web3 import Web3
from utils.crypto import decrypt
from connectors.exchange import BinanceConnector

# RPC endpoints
BSC_RPC = 'https://bsc-rpc.publicnode.com'
ETH_RPC = 'https://eth.llamarpc.com'

# Common tokens to check (name, address, decimals)
COMMON_TOKENS = {
    'bsc': [
        ('USDT', '0x55d398326f99059fF775485246999027B3197955', 18),
        ('USDC', '0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d', 18),
        ('BUSD', '0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56', 18),
    ],
    'eth': [
        ('USDT', '0xdAC17F958D2ee523a2206206994597C13D831ec7', 6),
        ('USDC', '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48', 6),
        ('DAI',  '0x6B175474E89094C44Da98b954EedeAC495271d0F', 18),
    ]
}

ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"}
]

def get_account_balance(account: dict) -> str:
    try:
        acc_type = account['type']
        if acc_type == 'bank':
            return _get_bank_balance(account)
        elif acc_type == 'exchange':
            return _get_exchange_balance(account)
        elif acc_type == 'wallet':
            return _get_wallet_balance(account)
        elif acc_type == 'savings':
            return _get_savings_balance(account)
        elif acc_type == 'forex':
            return _get_forex_balance(account)
        elif acc_type == 'stock':
                return _get_stock_balance(account)
        else:
            return f"{account['label']}: balance unavailable for this account type."



    except Exception as e:

        err_msg = str(e)

        if hasattr(e, 'response') and e.response is not None:

            try:

                err_data = e.response.json()

                err_msg = err_data.get('msg', err_msg)

            except:

                pass

        return f"{account['label']}: error ({err_msg})"

def _get_stock_balance(account):
    provider = account.get('provider', '').lower()
    if provider == 'bamboo':
        api_key = decrypt(account['api_key_encrypted'])
        connector = BambooConnector(api_key)
        balance = connector.get_balance()
        if not balance:
            return f"{account['label']}: no balance data."
        lines = [f"{k}: {v}" for k, v in balance.items()]
        return f"{account['label']} holdings:\n" + "\n".join(lines)
    else:
        return f"{account['label']}: stock provider not yet supported."

def _get_savings_balance(account):
    provider = account.get('provider', '').lower()
    if provider == 'piggyvest':
        # Use PiggyVest connector (mock for now)
        from connectors.piggyvest import PiggyVestConnector
        connector = PiggyVestConnector()
        balance = connector.get_balance()
        return f"{account['label']}: Total Savings ₦{balance.get('total_savings', 0):,.2f}"
    else:
        return f"{account['label']}: savings provider not yet supported."

def _get_forex_balance(account):
    provider = account.get('provider', '').lower()
    if provider == 'fxpro':
        api_key = decrypt(account['api_key_encrypted'])
        api_secret = decrypt(account['api_secret_encrypted'])
        from connectors.forex import FxProConnector
        connector = FxProConnector(api_key, api_secret)
        balance = connector.get_balance()
        if not balance:
            return f"{account['label']}: no forex balance data."
        # Format the forex balance nicely
        lines = [f"{k}: {v}" for k, v in balance.items()]
        return f"{account['label']} forex holdings:\n" + "\n".join(lines)
    else:
        return f"{account['label']}: forex provider not yet supported."


def _get_bank_balance(account):
    return f"{account['label']}: bank balance fetching coming soon."

def _get_exchange_balance(account):
    if account.get('provider') == 'binance':
        try:
            api_key = decrypt(account['api_key_encrypted'])
            api_secret = decrypt(account['api_secret_encrypted'])
            connector = BinanceConnector(api_key, api_secret)
            balances = connector.get_balances()
            if not balances:
                return f"{account['label']}: no balances found."
            lines = [f"{asset}: {data['free']}" for asset, data in balances.items() if data['free'] > 0]
            return f"{account['label']} holdings:\n" + "\n".join(lines)
        except Exception as e:
            err_msg = str(e).strip()
            if not err_msg:
                err_msg = "invalid API key or network error"
            return f"{account['label']}: error ({err_msg})"
    else:
        return f"{account['label']}: exchange not supported yet."

def _get_wallet_balance(account):
    address = account['wallet_address']
    network = account['network'].lower()
    lines = []
    # Native balance
    native = _get_evm_balance(address, network)
    lines.append(native)
    # Token balances
    tokens = _get_token_balances(address, network)
    if tokens:
        lines.append("Tokens: " + ", ".join(tokens))
    return "\n".join(lines)

def _get_evm_balance(address, network):
    try:
        if network == 'bsc':
            w3 = Web3(Web3.HTTPProvider(BSC_RPC))
            native = 'BNB'
        else:
            w3 = Web3(Web3.HTTPProvider(ETH_RPC))
            native = 'ETH'
        balance_wei = w3.eth.get_balance(Web3.to_checksum_address(address))
        balance = w3.from_wei(balance_wei, 'ether')
        return f"{network.upper()} Wallet ({address[:6]}...): {balance:.4f} {native}"
    except Exception as e:
        return f"{network.upper()} Wallet ({address[:6]}...): error ({str(e)})"

def _get_token_balances(address, network):
    net = network.lower()
    if net not in COMMON_TOKENS:
        return []
    w3 = Web3(Web3.HTTPProvider(BSC_RPC if net == 'bsc' else ETH_RPC))
    checksum_addr = Web3.to_checksum_address(address)
    results = []
    for name, token_addr, decimals in COMMON_TOKENS[net]:
        try:
            contract = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)
            raw = contract.functions.balanceOf(checksum_addr).call()
            if raw > 0:
                human = raw / (10 ** decimals)
                results.append(f"{name}: {human:.4f}")
        except Exception:
            pass
    return results

def _get_tron_balance(address, label):
    # Unused for now; placeholder for future TRC20 support
    api_key = os.environ.get("TRONGRID_API_KEY", "")
    url = f"https://api.trongrid.io/v1/accounts/{address}"
    headers = {"TRON-PRO-API-KEY": api_key} if api_key else {}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        if data.get("data"):
            balance_sun = data["data"][0].get("balance", 0)
            balance_trx = balance_sun / 1e6
            return f"{label}: {balance_trx:.4f} TRX"
        else:
            return f"{label}: address not found on Tron."
    except Exception as e:
        return f"{label}: error fetching TRX balance ({str(e)})"