# utils/crypto.py
import os
from cryptography.fernet import Fernet

def _get_cipher():
    key = os.environ.get("ENCRYPTION_KEY")
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except Exception:
        return None

def encrypt(plaintext: str) -> str:
    cipher = _get_cipher()
    if cipher is None:
        raise RuntimeError("ENCRYPTION_KEY not set or invalid – please set it on Render")
    return cipher.encrypt(plaintext.encode()).decode()

def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return None
    cipher = _get_cipher()
    if cipher is None:
        raise RuntimeError("ENCRYPTION_KEY not set or invalid")
    return cipher.decrypt(ciphertext.encode()).decode()