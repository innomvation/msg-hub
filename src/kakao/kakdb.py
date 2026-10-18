"""카카오톡 로컬 DB 읽기 — 루트 키 1개로 전체 방 DEK 일괄 도출 (26.8.2 64비트 대응).

핵심: 26.8.2(네이티브 64비트)에서 카톡은 v2 키저장소(appstate.dat, CBOR)에 방별 DEK를
RFC3394 AES-KW로 래핑해 둠. 래핑 해제 KEK는 HKDF-SHA256(salt=S, ikm=R, info="v2:"+relpath).
런타임 루트 키 R(32B)은 메모리에만 있음 → ICV(0xA6..) 검증으로 메모리에서 역산(≈4초).
R 하나로 **모든 방 키를 열지 않고** 도출 → 전 방 즉시 읽기, 팝업 없음.

26.8.1까진 WOW64(32비트)라 공개 도구가 됐지만 26.8.2 전환으로 다 깨졌던 것을, 루트 키
역산(ICV 검증)으로 해결. 본인 PC·본인 데이터, 메모리/DB 모두 읽기 전용.
appstate 구조/파생식 참고: getCurrentThread/kakaotalk-windows-forensics. 메모리읽기·SQLCipher: kakvendor(MIT).
"""
import os
import sys
import glob
import json
import struct
import bisect
import sqlite3
import tempfile
import threading
import time
import hmac
import hashlib
import winreg
import win32gui

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cbor2
from cryptography.hazmat.primitives.keywrap import aes_key_unwrap, InvalidUnwrap
from kakvendor import memkey, sqlcipher_v4

_LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
_lock = threading.Lock()
# root: 루트키 R / salt,prefix / entries: {relpath_bytes: wrapped_40B} / deks: {relpath_bytes: DEK} 캐시
_cache = {"root": None, "salt": None, "prefix": None, "entries": None,
          "deks": {}, "chat_data": None, "chatid": {}, "ts": 0, "users": None, "myid": None}


def _my_id():
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Kakao\KakaoTalk\UserAccounts")
        i = 0
        while True:
            sub = winreg.EnumKey(k, i); i += 1
            if sub.isdigit():
                return int(sub)
    except OSError:
        return None


MY_ID = _my_id()


def _find_chat_data():
    if _cache["chat_data"] and os.path.isdir(_cache["chat_data"]):
        return _cache["chat_data"]
    root = os.path.join(_LOCALAPPDATA, "Kakao", "KakaoTalk", "users")
    for d in glob.glob(os.path.join(root, "*", "chat_data")):
        if os.path.exists(os.path.join(d, "chatListInfo.edb")):
            _cache["chat_data"] = d
            return d
    return None


def _kek(salt, root, info):
    prk = hmac.new(salt, root, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:32]


def _find_root(chat_data):
    """appstate.dat 파싱 + 메모리에서 루트 키 R 역산(ICV 검증). 성공 시 캐시 채우고 True."""
    app = os.path.join(os.path.dirname(chat_data), "appstate.dat")
    if not os.path.exists(app):
        return False
    try:
        d = cbor2.load(open(app, "rb"))
    except Exception:
        return False
    tb = lambda x: bytes(x) if isinstance(x, (list, bytearray)) else x
    salt, prefix = tb(d["salt"]), tb(d["info_prefix"])
    entries = {tb(p): tb(c) for p, c in d["wrapped_dek_map"]}
    cl = next((p for p in entries if b"chatListInfo" in p), None)
    if not cl:
        return False
    info_cl, C_cl = prefix + cl, entries[cl]

    pids = memkey.find_pids()
    if not pids:
        return False
    h = memkey._open(pids[0])
    try:
        R = None
        for base, size, prot in memkey.iter_regions(h):
            if not (0 < size <= (256 << 20) and prot in (0x04, 0x40)):
                continue
            try:
                data = memkey.read_region(h, base, size)
            except Exception:
                continue
            for off in range(0, len(data) - 32, 16):
                w = data[off:off + 32]
                if w.count(0) >= 8 or len(set(w)) < 18:
                    continue
                try:
                    aes_key_unwrap(_kek(salt, w, info_cl), C_cl)
                except Exception:
                    continue
                R = w
                break
            if R:
                break
    finally:
        memkey.k32.CloseHandle(h)
    if not R:
        return False
    _cache.update(root=R, salt=salt, prefix=prefix, entries=entries, deks={}, ts=time.time())
    return True


def _ensure_root(force=False):
    chat_data = _find_chat_data()
    if not chat_data:
        return None
    if force or not _cache["root"]:
        if not _find_root(chat_data):
            return None
    return chat_data


def _dek_for_rel(relpath):
    """relpath(bytes, appstate의 원본 경로)의 DEK를 루트 키로 도출. 없으면 None."""
    if not _cache["root"]:
        return None
    if relpath in _cache["deks"]:
        return _cache["deks"][relpath]
    C = _cache["entries"].get(relpath)
    if not C:
        return None
    try:
        dek = aes_key_unwrap(_kek(_cache["salt"], _cache["root"], _cache["prefix"] + relpath), C)
    except Exception:
        return None
    _cache["deks"][relpath] = dek
    return dek


def _dek_for(fn):
    """chat_data 하위 파일(chatListInfo/chatLogs_*)의 DEK."""
    return _dek_for_rel(b"chat_data\\" + fn.encode("utf-8"))


def _resolve_myid(chat_data, memo_cid):
    """나와의 채팅(MemoChat)은 전부 내 메시지 → 그 authorId가 진짜 내 userId
    (레지스트리 계정번호와 다름)."""
    dek = _dek_for(f"chatLogs_{memo_cid}.edb")
    if not dek:
        return None
    tmp = _decrypt_tmp(chat_data, f"chatLogs_{memo_cid}.edb", dek, "memo")
    if not tmp:
        return None
    try:
        con = sqlite3.connect(tmp)
        row = con.execute(
            "SELECT authorId FROM chatLogs GROUP BY authorId ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None
    finally:
        try: os.remove(tmp)
        except OSError: pass


def _load_users(chat_data):
    """TalkUserDB.edb 복호화 → {userId: {"name":.., "img":..}} (프로필·발신자 실명). 캐시."""
    if _cache["users"] is not None:
        return _cache["users"]
    dek = _dek_for_rel(b"TalkUserDB.edb")   # 상위 폴더(users/{hash}) 파일, chat_data 아님
    base = os.path.dirname(chat_data)
    src = os.path.join(base, "TalkUserDB.edb")
    if not dek or not os.path.exists(src):
        _cache["users"] = {}
        return _cache["users"]
    tmp = os.path.join(tempfile.gettempdir(), "kakhub_users.sqlite")
    if not _decrypt_edb_wal(src, dek, tmp):
        tmp = None
    users = {}
    if tmp:
        try:
            con = sqlite3.connect(tmp)
            con.text_factory = lambda b: b.decode("utf-8", "replace")
            for uid, nick, fnick, img in con.execute(
                "SELECT userId, nickName, friendNickName, profileImageUrl FROM talkUser"
            ):
                users[uid] = {"name": (fnick or nick or ""), "img": img or ""}
            con.close()
        except Exception:
            pass
        finally:
            try: os.remove(tmp)
            except OSError: pass
    _cache["users"] = users
    return users


def _decrypt_edb_wal(src, dek, dst):
    """SQLCipher .edb 복호화 + .edb-wal(최신 미반영 쓰기)까지 반영해 최신 상태로.
    카톡은 수신 메시지를 WAL에 먼저 쓰므로, WAL을 안 읽으면 폰에서 온 최신 메시지가 누락됨."""
    try:
        data = open(src, "rb").read()
    except OSError:
        return False
    if len(data) < 4096:
        return False
    PS = 4096
    pages = {}
    for i, off in enumerate(range(0, len(data), PS), start=1):
        pg = data[off:off + PS]
        if len(pg) == PS:
            try:
                pages[i] = sqlcipher_v4.decrypt_page(pg, dek, i)
            except Exception:
                return False
    if not pages or pages.get(1, b"")[:16] != sqlcipher_v4.SQLITE_MAGIC:
        return False
    wal = src + "-wal"
    if os.path.exists(wal):
        try:
            wb = open(wal, "rb").read()
        except OSError:
            wb = b""
        if len(wb) >= 32 and int.from_bytes(wb[8:12], "big") == PS:
            off = 32
            while off + 24 + PS <= len(wb):
                pgno = int.from_bytes(wb[off:off + 4], "big")
                enc = wb[off + 24:off + 24 + PS]
                try:
                    pages[pgno] = sqlcipher_v4.decrypt_page(enc, dek, pgno)  # 최신 프레임이 덮어씀
                except Exception:
                    pass
                off += 24 + PS
    try:
        with open(dst, "wb") as f:
            for i in range(1, max(pages) + 1):
                f.write(pages.get(i, b"\x00" * PS))
    except OSError:
        return False
    return True


def _decrypt_tmp(chat_data, fn, dek, tag):
    tmp = os.path.join(tempfile.gettempdir(), f"kakhub_{tag}.sqlite")
    if _decrypt_edb_wal(os.path.join(chat_data, fn), dek, tmp):
        return tmp
    return None


def _fmt(v):
    try:
        v = int(v)
        if v > 1e12:
            v //= 1000
        return time.strftime("%H:%M", time.localtime(v))   # 날짜(10/17) 말고 항상 시:분
    except Exception:
        return ""


_WEEKDAY = ["월", "화", "수", "목", "금", "토", "일"]


def _fmt_date(v):
    """날짜 구분선용: '2026년 10월 18일 토요일'."""
    try:
        v = int(v)
        if v > 1e12:
            v //= 1000
        lt = time.localtime(v)
        return f"{lt.tm_year}년 {lt.tm_mon}월 {lt.tm_mday}일 {_WEEKDAY[lt.tm_wday]}요일"
    except Exception:
        return ""


def _sec(v):
    """lastUpdatedAt/sendAt → 초 단위 epoch (밀리초면 변환)."""
    try:
        v = int(v)
        return v // 1000 if v > 1e12 else v
    except Exception:
        return 0


# 방별 최신 메시지 캐시: cid → (chatLogs mtime, 미리보기텍스트, sendAt)
_lastmsg_cache = {}


def _log_mtime(chat_data, cid):
    """방 chatLogs(.edb/-wal)의 최근 수정시각(초). 메시지 도착 때마다 갱신됨."""
    mt = 0
    for suf in (".edb", ".edb-wal"):
        p = os.path.join(chat_data, f"chatLogs_{cid}{suf}")
        try:
            mt = max(mt, os.path.getmtime(p))
        except OSError:
            pass
    return int(mt)


def _latest_msg(chat_data, cid):
    """방 chatLogs 복호화 후 최신 메시지 (텍스트, sendAt). 없으면 None."""
    fn = f"chatLogs_{cid}.edb"
    dek = _dek_for(fn)
    if not dek:
        return None
    tmp = _decrypt_tmp(chat_data, fn, dek, f"lp_{cid}")
    if not tmp:
        return None
    try:
        con = sqlite3.connect(tmp)
        con.text_factory = lambda b: b.decode("utf-8", "replace")
        row = con.execute(
            "SELECT type, message, attachement, sendAt FROM chatLogs "
            "WHERE deleted=0 ORDER BY sendAt DESC LIMIT 1").fetchone()
        con.close()
        if row:
            return (_msg_text(row[0], row[1], row[2]), row[3])
    except Exception:
        return None
    finally:
        try: os.remove(tmp)
        except OSError: pass
    return None


# ───────────── 전체 방 목록 ─────────────
def get_all_rooms():
    with _lock:
        chat_data = _ensure_root()
        if not chat_data:
            return None
        dek = _dek_for("chatListInfo.edb")
        tmp = _decrypt_tmp(chat_data, "chatListInfo.edb", dek, "chatlist") if dek else None
        if not tmp:                       # 루트 키 만료(카톡 재시작) → 재역산 후 1회 재시도
            if not _ensure_root(force=True):
                return None
            dek = _dek_for("chatListInfo.edb")
            tmp = _decrypt_tmp(chat_data, "chatListInfo.edb", dek, "chatlist") if dek else None
            if not tmp:
                return None
        users = _load_users(chat_data)
        rooms, raw, chatid, memo_cid = [], [], {}, None
        try:
            con = sqlite3.connect(tmp)
            con.text_factory = lambda b: b.decode("utf-8", "replace")
            for cid, title, typ, lastmsg, unread, upd, alarm, fav, dmid in con.execute(
                "SELECT chatId, chatRoomTitle, type, lastChatMessage, newMessageCount, "
                "lastUpdatedAt, alarmOff, isFavorite, directChatMemberId FROM chatRoomList ORDER BY lastUpdatedAt DESC"
            ):
                name = (title or "").strip()
                if not name:
                    continue
                chatid[name] = cid
                if (typ or "") == "MemoChat":
                    memo_cid = cid
                img = users.get(dmid, {}).get("img", "") if dmid else ""   # 1:1은 상대 프로필
                # 카톡은 chatListInfo(목록 메타)를 디스크에 실시간으로 안 쓴다(며칠 밀리기도).
                # chatLogs 파일 수정시각으로 '진짜 최신 활동'을 잡아 순서/미리보기를 보정한다.
                up = _sec(upd)
                mt = _log_mtime(chat_data, cid)
                raw.append({
                    "name": name, "type": typ or "", "cid": cid,
                    "preview": (lastmsg or "").replace("\n", " ")[:80],
                    "unread": int(unread or 0),
                    "favorite": bool(fav), "muted": bool(alarm),
                    "image": img, "open": bool(win32gui.FindWindow(None, name)),
                    "pinned": (typ or "") == "MemoChat",   # 나와의 채팅 상단 고정
                    "_up": up, "_mt": mt, "_sort": max(up, mt),
                })
            members = {}                      # 읽음표시용: cid → {userId: (watermark, isActive)}
            try:
                for mcid, uid, wm, act in con.execute(
                        "SELECT chatId, userId, watermark, isActive FROM chatMembers"):
                    members.setdefault(mcid, {})[uid] = (int(wm or 0), int(act or 0))
            except Exception:
                pass
            _cache["members"] = members
            con.close()
        except Exception:
            return None
        finally:
            try: os.remove(tmp)
            except OSError: pass
        # 활성 방(파일이 목록 메타보다 최신)만 chatLogs에서 미리보기 갱신 — mtime 변화 시에만 복호화(캐시)
        budget = 20
        for r in sorted(raw, key=lambda x: x["_mt"], reverse=True):
            if r["_mt"] <= r["_up"] + 2:
                continue
            c = _lastmsg_cache.get(r["cid"])
            if c and c[0] == r["_mt"]:
                txt, sa = c[1], c[2]
            elif budget > 0:
                budget -= 1
                got = _latest_msg(chat_data, r["cid"])
                if not got:
                    continue
                txt, sa = got
                _lastmsg_cache[r["cid"]] = (r["_mt"], txt, sa)
            else:
                continue
            r["preview"] = (txt or "").replace("\n", " ")[:80]
            r["_sort"] = max(r["_sort"], _sec(sa))
        raw.sort(key=lambda x: (x["pinned"], x["_sort"]), reverse=True)
        for r in raw:
            r["time"] = _fmt(r["_sort"])
            for k in ("cid", "_up", "_mt", "_sort"):
                r.pop(k, None)
            rooms.append(r)
        _cache["chatid"] = chatid
        if _cache["myid"] is None and memo_cid is not None:
            _cache["myid"] = _resolve_myid(chat_data, memo_cid)
        return rooms


# ───────────── 방 메시지 (DB, 팝업 없이, 전 방) ─────────────
_MEDIA = {2: "[사진]", 3: "[동영상]", 5: "[음성]", 12: "[이모티콘]",
          20: "[사진]", 27: "[사진]", 6: "[연락처]", 17: "[위치]"}


def _msg_text(typ, message, attach):
    if typ in _MEDIA:
        return _MEDIA[typ]
    if typ == 18:
        try:
            return ("[파일] " + json.loads(attach or "{}").get("name", "")).strip()
        except Exception:
            return "[파일]"
    return message or ""


def _media(typ, attach):
    """메시지의 표시용 미디어(사진·동영상 썸네일·이모티콘). 없으면 None.
    프론트는 /media 로 받아 <img> 로 보여준다."""
    try:
        a = json.loads(attach or "{}")
    except Exception:
        return None
    if typ in (2, 20, 27):      # 사진(멀티는 대표 1장)
        u = a.get("url") or a.get("thumbnailUrl")
        return {"kind": "image", "url": u} if u else None
    if typ == 3:                # 동영상 → 썸네일
        u = a.get("thumbnailUrl") or a.get("url")
        return {"kind": "image", "url": u} if u else None
    if typ == 12:               # 이모티콘
        p = a.get("emoticonItemPath") or a.get("path")
        return {"kind": "emoticon", "path": p} if p else None
    if typ == 71:               # 피드/광고(선물하기 등) 템플릿 → 대표 썸네일
        try:
            for it in (a.get("C") or {}).get("ITL") or []:
                u = (it.get("TH") or {}).get("THU")
                if u:
                    return {"kind": "image", "url": u}
        except Exception:
            return None
    return None


def latest_sendat(room_name):
    """방 최신 메시지의 sendAt(초, 없으면 0). 미디어 전송 성공 확인용."""
    with _lock:
        chat_data = _ensure_root()
        if not chat_data:
            return 0
        cid = _cache["chatid"].get(room_name)
        if cid is None:
            return 0
        got = _latest_msg(chat_data, cid)
        return _sec(got[1]) if got else 0


def read_messages(room_name, limit=200):
    with _lock:
        chat_data = _ensure_root()
        if not chat_data:
            return None
        cid = _cache["chatid"].get(room_name)
        if cid is None:
            return None
        fn = f"chatLogs_{cid}.edb"
        if not os.path.exists(os.path.join(chat_data, fn)):
            return None
        dek = _dek_for(fn)
        if not dek:
            return None
        tmp = _decrypt_tmp(chat_data, fn, dek, f"log_{cid}")
        if not tmp:
            if not _ensure_root(force=True):
                return None
            dek = _dek_for(fn)
            tmp = _decrypt_tmp(chat_data, fn, dek, f"log_{cid}") if dek else None
            if not tmp:
                return None
        msgs = []
        try:
            con = sqlite3.connect(tmp)
            con.text_factory = lambda b: b.decode("utf-8", "replace")
            rows = con.execute(
                "SELECT logId, authorId, type, message, attachement, sendAt FROM chatLogs "
                "WHERE deleted=0 ORDER BY sendAt DESC LIMIT ?", (limit,)
            ).fetchall()
            con.close()
            users = _load_users(chat_data)
            myid = _cache["myid"] or MY_ID
            # 읽음표시: 상대 멤버의 '실질 watermark' = max(DB watermark, 그 사람 마지막 메시지 logId).
            # watermark는 디스크 반영이 밀려서, 상대가 보낸 마지막 메시지 logId로 보정한다.
            members = _cache.get("members", {}).get(cid, {})
            last_by_user = {}
            for lid, aid, *_ in rows:
                if lid and lid > last_by_user.get(aid, 0):
                    last_by_user[aid] = lid
            others = {uid: max(wm, last_by_user.get(uid, 0))
                      for uid, (wm, act) in members.items() if uid != myid and act}
            for lid, aid, typ, message, attach, sendat in reversed(rows):
                mine = (aid == myid)
                info = users.get(aid, {})
                sender = "나" if mine else (info.get("name") or room_name)
                unread = sum(1 for ewm in others.values() if ewm < lid) if (mine and lid and others) else 0
                msgs.append({"mine": mine, "sender": sender,
                             "img": "" if mine else info.get("img", ""),
                             "text": _msg_text(typ, message, attach),
                             "media": _media(typ, attach),
                             "time": _fmt(sendat), "date": _fmt_date(sendat), "type": typ,
                             "unread_count": unread})
        except Exception:
            return None
        finally:
            try: os.remove(tmp)
            except OSError: pass
        return msgs
