"""Thin AES-CBC wrapper built on the `cryptography` library (pycryptodome-free).

KakaoTalk's Windows scheme only ever uses AES-CBC. We never rely on library
padding here — the caller decides block alignment — because the .edb page
decryption uses *no* padding (page size is a multiple of 16).
"""
from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

_BACKEND = default_backend()


def aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """Raw AES-CBC encrypt. `data` must be a multiple of 16 bytes."""
    enc = Cipher(algorithms.AES(key), modes.CBC(iv), backend=_BACKEND).encryptor()
    return enc.update(data) + enc.finalize()


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """Raw AES-CBC decrypt. `data` must be a multiple of 16 bytes."""
    dec = Cipher(algorithms.AES(key), modes.CBC(iv), backend=_BACKEND).decryptor()
    return dec.update(data) + dec.finalize()
