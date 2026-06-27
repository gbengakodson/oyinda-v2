# utils/crypto.py
import os
from cryptography.fernet import Fernet

# Generate a key once and store in env var: ENCRYPTION_KEY = Fernet.generate_key()
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY")
if not ENCRYPTION_KEY:
    raise RuntimeError("ENCRYPTION_KEY environment variable not set")

cipher = Fernet(ENCRYPTION_KEY.encode())

def encrypt(plaintext: str) -> str:
    return cipher.encrypt(plaintext.encode()).decode()

def decrypt(ciphertext: str) -> str:
    return cipher.decrypt(ciphertext.encode()).decode()