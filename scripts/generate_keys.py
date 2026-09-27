"""Erzeugt sichere Werte für SECRET_KEY und ENCRYPTION_KEY (zum Einfügen in die .env)."""
import secrets

from cryptography.fernet import Fernet

print(f"SECRET_KEY={secrets.token_urlsafe(48)}")
print(f"ENCRYPTION_KEY={Fernet.generate_key().decode()}")
