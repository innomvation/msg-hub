"""Extract the derived AES key/iv from the *running* KakaoTalk process memory.

Why this works without the hardcoded key:
  KakaoTalk must hold the derived key to read its own .edb at runtime. The same
  key/iv decrypt every .edb of the account (key = md5((pragma+userId)[:512]),
  independent of the file). So we scan committed memory, treat each aligned
  window as a candidate AES key, derive iv = md5(base64(key)), and test it
  against the known SQLite-header oracle on a real .edb first page. One hit =
  we can decrypt all files, now and later. Read-only; never writes to the process.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import hashlib
import time
from dataclasses import dataclass
from typing import Iterable, Iterator, List, Optional, Tuple

from .keyderiv import check_oracle, SQLITE_MAGIC

# --- Win32 --------------------------------------------------------------
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}  # RO, RW, WC, XR, XRW, XWC

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("__a1", wt.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
        ("__a2", wt.DWORD),
    ]


def find_pids(name: str = "KakaoTalk.exe") -> List[int]:
    arr = (wt.DWORD * 4096)()
    need = wt.DWORD()
    psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(need))
    count = need.value // ctypes.sizeof(wt.DWORD)
    out = []
    PROCESS_QUERY_LIMITED = 0x1000
    for i in range(count):
        pid = arr[i]
        if not pid:
            continue
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
        if not h:
            continue
        buf = ctypes.create_unicode_buffer(260)
        size = wt.DWORD(260)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            if buf.value.lower().endswith(name.lower()):
                out.append(pid)
        k32.CloseHandle(h)
    return out


def _open(pid: int):
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        raise OSError(f"OpenProcess({pid}) failed: {ctypes.get_last_error()}")
    return h


def iter_regions(h) -> Iterator[Tuple[int, int, int]]:
    mbi = MEMORY_BASIC_INFORMATION()
    addr = 0
    max_addr = 0x7FFFFFFFFFFF
    while addr < max_addr:
        r = k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
        if not r:
            break
        size = mbi.RegionSize
        if (mbi.State == MEM_COMMIT and mbi.Protect in READABLE
                and not (mbi.Protect & PAGE_GUARD)):
            yield addr, size, mbi.Protect
        addr += size or 0x1000


def read_region(h, base: int, size: int, chunk: int = 8 << 20) -> bytes:
    out = bytearray()
    buf = ctypes.create_string_buffer(chunk)
    got = ctypes.c_size_t(0)
    off = 0
    while off < size:
        n = min(chunk, size - off)
        ok = k32.ReadProcessMemory(h, ctypes.c_void_p(base + off),
                                   buf, n, ctypes.byref(got))
        if ok and got.value:
            out += buf.raw[:got.value]
        else:
            out += b"\x00" * n  # keep offsets aligned across gaps
        off += n
    return bytes(out)


# --- scanning -----------------------------------------------------------
@dataclass
class MemKey:
    key: bytes
    iv: bytes
    address: int


def _iv_for(key: bytes) -> bytes:
    return hashlib.md5(base64.b64encode(key)).digest()


def _scan_data(data: bytes, base: int, first_page: bytes,
               key_sizes=(16,), step: int = 4) -> Optional[MemKey]:
    n = len(data)
    for ks in key_sizes:
        limit = n - ks
        i = 0
        while i <= limit:
            key = data[i:i + ks]
            if key and key != b"\x00" * ks and key != key[:1] * ks:
                iv = _iv_for(key)
                if check_oracle(key, iv, first_page):
                    return MemKey(key, iv, base + i)
            i += step
    return None


def scan(pid: int, first_page: bytes, key_sizes=(16,), step: int = 4,
         progress=None) -> Optional[MemKey]:
    h = _open(pid)
    scanned = 0
    t0 = time.time()
    try:
        for base, size, _prot in iter_regions(h):
            if size <= 0 or size > (512 << 20):
                continue
            data = read_region(h, base, size)
            mk = _scan_data(data, base, first_page, key_sizes, step)
            if mk:
                return mk
            n = len(data)
            scanned += n
            if progress and scanned:
                rate = scanned / (time.time() - t0 + 1e-9) / (1 << 20)
                progress(scanned, rate)
    finally:
        k32.CloseHandle(h)
    return None


def scan_near(pid: int, first_page: bytes, needles: Iterable[str],
              key_sizes=(16,), step: int = 1, radius: int = 4 << 20,
              max_region: int = 128 << 20, progress=None) -> Optional[MemKey]:
    """Scan candidate keys only near high-value textual memory markers.

    The full-memory scan is very slow in Python because every offset becomes an
    AES trial key.  KakaoTalk usually keeps DB paths, user-dir paths, and DB
    worker state in the same committed regions, so this focused pass checks
    windows around those markers first.
    """
    h = _open(pid)
    raw_needles: List[bytes] = []
    for needle in needles:
        if not needle:
            continue
        raw_needles.append(needle.encode("utf-8", "ignore"))
        raw_needles.append(needle.encode("utf-16-le", "ignore"))
    raw_needles = sorted(set(raw_needles), key=len, reverse=True)
    scanned = 0
    windows = 0
    t0 = time.time()
    try:
        for base, size, _prot in iter_regions(h):
            if size <= 0 or size > max_region:
                continue
            data = read_region(h, base, size)
            ranges: List[Tuple[int, int]] = []
            for needle in raw_needles:
                pos = 0
                while True:
                    pos = data.find(needle, pos)
                    if pos < 0:
                        break
                    lo = max(0, pos - radius)
                    hi = min(len(data), pos + len(needle) + radius)
                    ranges.append((lo, hi))
                    pos += max(1, len(needle))
            if not ranges:
                continue
            ranges.sort()
            if progress:
                rate = scanned / (time.time() - t0 + 1e-9) / (1 << 20)
                progress(scanned, rate, windows)
            merged: List[Tuple[int, int]] = []
            for lo, hi in ranges:
                if not merged or lo > merged[-1][1]:
                    merged.append((lo, hi))
                elif hi > merged[-1][1]:
                    merged[-1] = (merged[-1][0], hi)
            for lo, hi in merged:
                chunk = data[lo:hi]
                mk = _scan_data(chunk, base + lo, first_page, key_sizes, step)
                if mk:
                    return mk
                scanned += len(chunk)
                windows += 1
                if progress:
                    rate = scanned / (time.time() - t0 + 1e-9) / (1 << 20)
                    progress(scanned, rate, windows)
    finally:
        k32.CloseHandle(h)
    return None


def probe(pid: int) -> Tuple[int, int]:
    """Return (region_count, total_readable_bytes) for feasibility estimate."""
    h = _open(pid)
    cnt = tot = 0
    try:
        for _b, size, _p in iter_regions(h):
            cnt += 1
            tot += size
    finally:
        k32.CloseHandle(h)
    return cnt, tot
