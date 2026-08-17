import os
from web3 import Web3

def get_web3():
    rpc = os.getenv('BSC_RPC_URL', 'https://bsc-dataseed.binance.org/')
    return Web3(Web3.HTTPProvider(rpc))

def send_usdt(to_address, amount_wei):
    """Send USDT on BSC. amount_wei is integer in smallest unit (18 decimals)."""
    w3 = get_web3()
    private_key = os.getenv('HOT_WALLET_PRIVATE_KEY')
    sender = os.getenv('HOT_WALLET_ADDRESS')
    contract_address = os.getenv('USDT_BEP20_CONTRACT', '0x55d398326f99059fF775485246999027B3197955')

    # Minimal USDT ERC20 ABI (transfer only)
    usdt_abi = [
        {
            "constant": False,
            "inputs": [
                {"name": "_to", "type": "address"},
                {"name": "_value", "type": "uint256"}
            ],
            "name": "transfer",
            "outputs": [{"name": "", "type": "bool"}],
            "type": "function"
        }
    ]

    contract = w3.eth.contract(address=Web3.to_checksum_address(contract_address), abi=usdt_abi)
    nonce = w3.eth.get_transaction_count(sender)
    tx = contract.functions.transfer(
        Web3.to_checksum_address(to_address),
        amount_wei
    ).build_transaction({
        'chainId': 56,
        'gas': 60000,
        'gasPrice': w3.eth.gas_price,
        'nonce': nonce,
    })

    signed_tx = w3.eth.account.sign_transaction(tx, private_key=private_key)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
    return w3.to_hex(tx_hash)