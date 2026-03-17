"""
Cifrado simétrico (Fernet) para las contraseñas SRI de los tenants.

La FERNET_KEY debe generarse una sola vez y guardarse en .env:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Nunca se almacena la contraseña en texto plano ni se devuelve por API.
"""

from __future__ import annotations

from cryptography.fernet import Fernet

from .config import settings

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(settings.fernet_key.encode())
    return _fernet


def encrypt(plain: str) -> str:
    return _get_fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    return _get_fernet().decrypt(token.encode()).decode()
