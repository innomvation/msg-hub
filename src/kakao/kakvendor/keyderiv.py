"""KakaoTalk (Windows) key/iv derivation, with an oracle that auto-resolves the
small ambiguities the public write-ups leave open.

Algorithm (from J. Cho & N. Jang, JKIISC 2023.02; confirmed against real files):

  PRAGMA:
    src   = f"{sys_uuid}|{hdd_model}|{hdd_serial}"
    ct    = AES-128-CBC(hardcoded_key, iv=0, pad(src))
    pragma= base64( sha512(ct) )

  DB key/iv:
    buf   = (pragma + str(userId)) repeated until >= 512 bytes, truncated to 512
    key   = md5(buf)                 # 16 bytes -> AES-128
    iv    = md5( base64(key) )        # 16 bytes

  Decrypt each 4096-byte page with AES-128-CBC(key, iv), no padding, iv reset
  per page. Page 0's first 16 plaintext bytes are always b"SQLite format 3\\x00"
  — that is our verification oracle.

The one value NOT derivable from the machine is `hardcoded_key` (a constant
baked into KakaoTalk.exe, unpublished). Supply it via config; everything else
here is complete and self-verifying.
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from itertools import product
from typing import Iterable, List, Optional, Tuple

from .aes import aes_cbc_encrypt, aes_cbc_decrypt

SQLITE_MAGIC = b"SQLite format 3\x00"  # 16 bytes — the oracle


def _pad_variants(src: bytes) -> List[Tuple[str, bytes]]:
    """Candidate block-alignments for the pragma source string.

    The reference snippet calls AES.encrypt(src) directly, which only works when
    len(src) % 16 == 0. Real device strings are rarely aligned, so KakaoTalk pads
    somehow. We try the plausible strategies and let the oracle decide.
    """
    n = len(src)
    pad = (-n) % 16
    out = [("zero", src + b"\x00" * pad),
           ("space", src + b" " * pad),
           ("pkcs7", src + bytes([pad or 16]) * (pad or 16)),
           ("truncate", src[: n - (n % 16)] if n % 16 else src)]
    # de-dup while keeping order
    seen, uniq = set(), []
    for name, b in out:
        if b and len(b) % 16 == 0 and b not in seen:
            seen.add(b)
            uniq.append((name, b))
    return uniq


def pragma_candidates(pragma_source: str, hardcoded_key: bytes) -> List[Tuple[str, str]]:
    """Return [(label, pragma_string)] candidates for the given device string."""
    iv0 = b"\x00" * 16
    src = pragma_source.encode("utf-8")
    cands: List[Tuple[str, str]] = []
    for pad_name, block in _pad_variants(src):
        ct = aes_cbc_encrypt(hardcoded_key, iv0, block)
        b64ct = base64.b64encode(ct)
        forms = {
            "b64(sha512(b64(ct)))": base64.b64encode(hashlib.sha512(b64ct).digest()).decode(),
            "sha512hex(b64(ct))": hashlib.sha512(b64ct).hexdigest(),
            "b64(sha512(ct))": base64.b64encode(hashlib.sha512(ct).digest()).decode(),
            "b64(ct)": b64ct.decode(),
        }
        for form_name, pragma in forms.items():
            cands.append((f"{pad_name}|{form_name}", pragma))
    return cands


def pragma_fig3(pragma_source: str, hardcoded_key: bytes) -> str:
    """The paper's exact Fig.3 pragma (confirmed against the published algorithm):
       base64( sha512( AES-128-CBC-PKCS7(hardcoded_key, iv=0,
                                        "uuid|model|serial") ) ).
    Use this fast path first; fall back to pragma_candidates() only if it misses.
    """
    src = pragma_source.encode("utf-8")
    pad = 16 - (len(src) % 16) or 16  # PKCS#7 always pads (full block if aligned)
    block = src + bytes([pad]) * pad
    ct = aes_cbc_encrypt(hardcoded_key, b"\x00" * 16, block)
    return base64.b64encode(hashlib.sha512(ct).digest()).decode()


def generate_key_iv(pragma: str, user_id: str) -> Tuple[bytes, bytes]:
    buf = (pragma + user_id).encode("utf-8")
    if not buf:
        raise ValueError("empty pragma+userId")
    while len(buf) < 512:
        buf += buf
    buf = buf[:512]
    key = hashlib.md5(buf).digest()
    iv = hashlib.md5(base64.b64encode(key)).digest()
    return key, iv


def check_oracle(key: bytes, iv: bytes, first_page: bytes) -> bool:
    """True if (key, iv) decrypt the first .edb page to a SQLite header."""
    try:
        dec = aes_cbc_decrypt(key, iv, first_page[:16])
    except Exception:
        return False
    return dec == SQLITE_MAGIC


@dataclass
class Solution:
    pragma_label: str
    pragma: str
    user_id: str
    key: bytes
    iv: bytes


def solve(pragma_source: str,
          hardcoded_key: bytes,
          first_page: bytes,
          user_ids: Iterable[str]) -> Optional[Solution]:
    """Find the (pragma-variant, userId) combination that satisfies the oracle.

    `user_ids` is any iterable of candidate ids (a known id, a shortlist, or a
    brute-force range rendered as strings).
    """
    cands = pragma_candidates(pragma_source, hardcoded_key)
    for uid in user_ids:
        for label, pragma in cands:
            key, iv = generate_key_iv(pragma, uid)
            if check_oracle(key, iv, first_page):
                return Solution(label, pragma, uid, key, iv)
    return None
