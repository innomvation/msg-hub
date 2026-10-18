"""Small SQLCipher v4 page-oracle helpers for KakaoTalk v2 DB research.

This is not a full SQLCipher implementation.  It only decrypts enough of page 1
to tell whether a candidate 32-byte raw key is plausible.

Observed current KakaoTalk builds carry SQLCipher strings in memory and store
v2 per-file wrapped DEKs in appstate.dat.  SQLCipher v4 defaults are:

  page_size=4096, reserve=80, HMAC_SHA512, PBKDF2_HMAC_SHA512

For page 1, bytes 0..15 in the encrypted file are the KDF salt.  The encrypted
SQLite header begins at byte 16 and should decrypt to the rest of a normal
SQLite database header.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PAGE_SIZE = 4096
RESERVE_SHA512 = 80
SQLITE_MAGIC = b"SQLite format 3\x00"


def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def _xor_byte(data: bytes, mask: int) -> bytes:
    return bytes(b ^ mask for b in data)


def _hmac_key(cipher_key: bytes, salt: bytes, fast_iter: int = 2,
              digest: str = "sha512") -> bytes:
    # SQLCipher derives a separate HMAC key from the cipher key and salt^0x3a.
    return hashlib.pbkdf2_hmac(digest, cipher_key, _xor_byte(salt, 0x3A),
                               fast_iter, dklen=len(cipher_key))


def _looks_like_sqlite_header_tail(tail: bytes) -> bool:
    """Validate bytes 16..100 of a SQLite database header."""
    if len(tail) < 84:
        return False
    page_size = int.from_bytes(tail[0:2], "big")
    if page_size not in {512, 1024, 2048, 4096, 8192, 16384, 32768, 65536}:
        return False
    if tail[2] not in (1, 2) or tail[3] not in (1, 2):
        return False
    if tail[4] < 32 or tail[5] < 32 or tail[6] < 32:
        return False
    # schema format number at database-header offset 44, i.e. tail[28:32].
    schema_format = int.from_bytes(tail[28:32], "big")
    if schema_format not in (0, 1, 2, 3, 4):
        return False
    return True


@dataclass
class OracleHit:
    key: bytes
    variant: str
    header_tail: bytes


def try_raw_key(first_page: bytes, key: bytes,
                reserve_values: Iterable[int] = (RESERVE_SHA512, 48, 32, 16, 0),
                page_size: int = PAGE_SIZE) -> Optional[OracleHit]:
    """Return hit if key decrypts page 1 like SQLCipher raw-key mode.

    It tests the common SQLCipher layout where page-1 salt occupies bytes 0..15
    and the IV is stored in the reserve trailer.
    """
    if len(key) not in (16, 24, 32) or len(first_page) < page_size:
        return None
    salt = first_page[:16]
    for reserve in reserve_values:
        usable = page_size - reserve
        if usable <= 16 or usable > len(first_page):
            continue
        # SQLCipher with HMAC stores IV as the first 16 bytes of the reserve
        # trailer.  With reserve=0 there is no trailer, so skip unless a caller
        # adds a custom IV mode later.
        if reserve < 16:
            continue
        enc = first_page[16:usable]
        iv = first_page[usable:usable + 16]
        if len(enc) % 16:
            continue
        try:
            dec_tail = _aes_cbc_decrypt(key, iv, enc[:96])
        except Exception:
            continue
        if _looks_like_sqlite_header_tail(dec_tail[:84]):
            return OracleHit(key, f"raw/aes-cbc/page1/reserve={reserve}", dec_tail)
    return None


def hmac_check_page(first_page: bytes, key: bytes, pgno: int = 1,
                    reserve: int = RESERVE_SHA512, page_size: int = PAGE_SIZE,
                    hmac_algo: str = "sha512") -> bool:
    """Best-effort SQLCipher HMAC check for a candidate raw key."""
    if len(first_page) < page_size or reserve < 80:
        return False
    salt = first_page[:16]
    usable = page_size - reserve
    mac_region = first_page[:usable + 16]  # encrypted data + IV trailer
    stored = first_page[usable + 16:usable + 80]
    hk = _hmac_key(key, salt, digest=hmac_algo)
    # SQLCipher page number is normally little-endian by default.
    for endian in ("little", "big"):
        msg = mac_region + pgno.to_bytes(4, endian)
        got = hmac.new(hk, msg, getattr(hashlib, hmac_algo)).digest()
        if hmac.compare_digest(got, stored[:len(got)]):
            return True
    return False


def decrypt_page(page: bytes, key: bytes, pgno: int,
                 reserve: int = RESERVE_SHA512,
                 page_size: int = PAGE_SIZE) -> bytes:
    """Decrypt one SQLCipher v4 page to a normal SQLite page.

    The output keeps SQLite's reserved trailer bytes as zeroes; SQLite accepts
    that because the database header declares the same reserved-byte count.
    """
    if len(page) < page_size:
        return page
    usable = page_size - reserve
    iv = page[usable:usable + 16]
    if pgno == 1:
        plain = SQLITE_MAGIC + _aes_cbc_decrypt(key, iv, page[16:usable])
    else:
        plain = _aes_cbc_decrypt(key, iv, page[:usable])
    return plain + (b"\x00" * reserve)


def decrypt_bytes(data: bytes, key: bytes,
                  reserve: int = RESERVE_SHA512,
                  page_size: int = PAGE_SIZE) -> bytes:
    out = bytearray()
    pgno = 1
    for off in range(0, len(data), page_size):
        page = data[off:off + page_size]
        if len(page) == page_size:
            out += decrypt_page(page, key, pgno, reserve, page_size)
        else:
            out += page
        pgno += 1
    return bytes(out)


def decrypt_file(src: str, dst: str, key: bytes,
                 reserve: int = RESERVE_SHA512,
                 page_size: int = PAGE_SIZE) -> bool:
    data = open(src, "rb").read()
    dec = decrypt_bytes(data, key, reserve, page_size)
    with open(dst, "wb") as f:
        f.write(dec)
    return dec[:16] == SQLITE_MAGIC
