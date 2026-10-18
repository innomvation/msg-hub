"""카톡 패널용 로컬 백엔드.

kakao_lib(로컬 UI 자동화)을 감싸 localhost HTTP로 노출한다. Electron 앱이 이 서버를 띄우고
카톡 패널에서 fetch 한다.

  GET  /rooms          → 현재 열려있는 카톡 방 창 목록
  GET  /read?room=NAME → 그 방의 메시지 [{sender,time,text}, ...]
  POST /send {room,text} → 메시지 전송

열려있는 방만 다루므로(open_if_needed=False) 방을 자동으로 열지 않는다 → 포커스 안 뺏음.
"""
import sys
import os
import re
import json
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# 옆 폴더의 kakao-automation 에서 kakao_lib 를 가져온다
KAKAO_LIB_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "kakao-automation")
)
sys.path.insert(0, KAKAO_LIB_DIR)
import kakao_lib  # noqa: E402
import win32gui  # noqa: E402
import win32con  # noqa: E402
import win32api  # noqa: E402
import ctypes  # noqa: E402


# ── 입력 꼬임 방지 안전장치 ──
# 카톡 자동화(방 열기 Ctrl+F, 전송)가 Ctrl/Alt 같은 보조키를 누른 채 남기면
# 마우스/키보드가 통째로 먹통이 될 수 있다. 자동화 직후 항상 호출해 확실히 풀어준다.
_MODIFIER_VKS = (0x11, 0xA2, 0xA3, 0x12, 0xA4, 0xA5, 0x10, 0xA0, 0xA1, 0x5B, 0x5C)  # Ctrl/Alt/Shift/Win L·R


def _release_input():
    try:
        ctypes.windll.user32.BlockInput(False)
    except Exception:
        pass
    for k in _MODIFIER_VKS:
        try:
            win32api.keybd_event(k, 0, win32con.KEYEVENTF_KEYUP, 0)
        except Exception:
            pass

try:
    import kakdb  # 전체 방 목록 (로컬 DB 복호화, 26.8.2 64비트 대응)
except Exception as _e:  # noqa: F841
    kakdb = None

MY_NAME = os.getenv("KAKAO_MY_NAME", "나")  # 자동응답에서 '내' 메시지 구분용(본인 카톡 표시이름)
LINE_RE = re.compile(r"^\[([^\]]+)\] \[([^\]]+)\] (.*)$")
_lock = threading.Lock()  # 클립보드/키보드 상태를 건드리므로 한 번에 하나씩만

# 자동응답용 LLM — 자동응답을 켤 때만 실제 호출. .env 경로는 환경변수로 지정.
_ENV = os.getenv("KAKAO_LLM_ENV", "")
_llm = None
_LLM_MODEL = "qwen3.5-9b"


def _get_llm():
    global _llm
    if _llm is None:
        from dotenv import load_dotenv
        from openai import OpenAI
        load_dotenv(_ENV)
        _llm = OpenAI(base_url="https://agent.kau.ac.kr/v1", api_key=os.getenv("KAU_API_KEY"))
    return _llm


CONFIG_PATH = os.path.join(os.path.dirname(__file__), "rooms.json")


def _load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f).get("rooms", [])
    except Exception:
        return []


def _save_config(rooms):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"rooms": rooms}, f, ensure_ascii=False, indent=2)


def _open_titles():
    out = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetClassName(h) == "EVA_Window_Dblclk":
            t = win32gui.GetWindowText(h)
            if t and t != "카카오톡":
                out.append(t)

    win32gui.EnumWindows(cb, None)
    return out


def _ensure_open_bg(room):
    """방 창이 없으면 연다. 열자마자 최소화하고 포커스를 원래 창으로 되돌려
    카톡이 화면에 머물거나 포커스를 뺏지 않게 한다(처음 열 때 잠깐 반짝은 불가피).
    주의: 호출 측이 _lock 을 잡은 상태로 부른다(여기선 다시 잠그지 않음)."""
    if win32gui.FindWindow(None, room):
        return
    prev = win32gui.GetForegroundWindow()
    main = win32gui.FindWindow(None, "카카오톡")
    # 원래 안 보이던(트레이 숨김 or 최소화) 상태면 전송 후 다시 내린다.
    # IsIconic만 보면 트레이 상주(숨김)를 못 잡아 메인 창이 화면에 남는다.
    main_hidden = bool(main) and (win32gui.IsIconic(main) or not win32gui.IsWindowVisible(main))
    kakao_lib.open_room(room)
    h = win32gui.FindWindow(None, room)
    if h:
        win32gui.ShowWindow(h, win32con.SW_MINIMIZE)  # 방 창 안 보이게
    if main and main_hidden:
        win32gui.ShowWindow(main, win32con.SW_MINIMIZE)  # 원래 숨겨져 있었으면 다시 내림
    if prev:
        try:
            win32gui.SetForegroundWindow(prev)  # 포커스 원래 창으로
        except Exception:
            pass


# 알림으로 잡힌 방: room -> {"text":..., "time":...} (허브 켜있는 동안 누적)
_notified = {}
_notif_lock = threading.Lock()
NOTIF_PS = os.path.join(os.path.dirname(__file__), "notif.ps1")


def _poll_notifications():
    import subprocess
    while True:
        try:
            out = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", NOTIF_PS],
                capture_output=True, timeout=30,
            )
            data = json.loads(out.stdout.decode("utf-8", "replace").strip() or "[]")
            with _notif_lock:
                for n in data:
                    room = (n.get("title") or "").strip()
                    if not room:
                        continue
                    t = n.get("time", "")
                    try:
                        hhmm = __import__("datetime").datetime.fromisoformat(t).strftime("%H:%M")
                    except Exception:
                        hhmm = ""
                    _notified[room] = {"text": n.get("body", ""), "time": hhmm}
        except Exception as e:
            print("[notif] 실패:", e, flush=True)
        time.sleep(6)


def list_rooms():
    """등록한 방(config) + 알림으로 온 방 + 지금 열린 방을 합쳐 반환."""
    open_set = set(_open_titles())
    with _notif_lock:
        notified = dict(_notified)
    names = list(dict.fromkeys(_load_config() + list(notified.keys()) + list(open_set)))
    out = []
    for n in names:
        info = notified.get(n, {})
        out.append({"name": n, "open": n in open_set,
                    "preview": info.get("text", ""), "time": info.get("time", "")})
    return out


def read_messages(room, limit=100, allow_open=False):
    with _lock:
        try:
            if allow_open:
                _ensure_open_bg(room)  # 닫혀 있으면 백그라운드로 열기(최소화+포커스 복귀)
            text = kakao_lib.read_room(room, open_if_needed=False)
        except RuntimeError:
            return []  # 닫힌 방(미리보기 등)은 조용히 빈 값
        finally:
            _release_input()
    msgs = []
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if m:
            sender, t, msg = m.groups()
            msgs.append({"sender": sender, "time": t, "text": msg})
    return msgs[-limit:]


def send(room, text):
    with _lock:
        try:
            _ensure_open_bg(room)   # 닫혀있으면 백그라운드로 열고(최소화+포커스복귀) 전송
            kakao_lib.send_text(room, text, open_if_needed=False)
        finally:
            _release_input()        # 눌린 보조키 확실히 해제(먹통 방지)


def send_media(room, path):
    """사진/파일 전송. 클립보드 붙여넣기+키보드 방식이라 타이밍상 한 번에 실패할 수 있어,
    DB로 새 메시지 도착을 확인하고 안 왔으면 재시도한다(결과 보장). 성공 후 방 창은 최소화."""
    before = 0
    if kakdb is not None:
        try:
            before = kakdb.latest_sendat(room) or 0
        except Exception:
            before = 0
    ok = False
    try:
        for _ in range(2):   # 포커스 뺏는 횟수 최소화(먹통 위험 줄임)
            with _lock:
                try:
                    kakao_lib.send_media(room, path, open_if_needed=True)
                finally:
                    _release_input()   # 매 시도 후 눌린 보조키 해제
            time.sleep(1.6)
            if kakdb is None:
                ok = True
                break
            try:
                if (kakdb.latest_sendat(room) or 0) > before:
                    ok = True
                    break
            except Exception:
                pass
        with _lock:
            h = win32gui.FindWindow(None, room)
            if h:
                win32gui.ShowWindow(h, win32con.SW_MINIMIZE)
    finally:
        _release_input()
    if not ok:
        raise RuntimeError("사진 전송 실패(재시도 초과)")


def send_media_bytes(room, filename, data):
    """업로드된 파일 바이트를 임시파일로 저장한 뒤 전송하고 지운다."""
    import tempfile
    ext = os.path.splitext(filename)[1] or ".png"
    fd, tmp = tempfile.mkstemp(suffix=ext)
    os.close(fd)
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        send_media(room, tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ─────────── 자동응답 (방별 ON/OFF) ───────────
_auto = {}  # room -> threading.Event (set 되면 중지)


def _generate(log_text):
    msgs = [LINE_RE.match(l).groups() for l in log_text.splitlines() if LINE_RE.match(l)]
    convo = "\n".join(f"{s}: {m}" for s, _t, m in msgs[-15:])
    r = _get_llm().chat.completions.create(
        model=_LLM_MODEL,
        messages=[
            {"role": "system", "content": "너는 이 카톡방의 '나'로서 대화해. 반말로 1~2문장 짧고 자연스럽게, 마지막에 온 메시지에만 답해."},
            {"role": "user", "content": convo + "\n\n위 대화에서 '나'로서 마지막 메시지에 답장만 써."},
        ],
        max_tokens=150, temperature=0.7,
    )
    return r.choices[0].message.content.strip()


def _auto_loop(room, stop):
    try:
        with _lock:
            seen = set(kakao_lib.read_room(room, open_if_needed=False).splitlines())
    except Exception:
        seen = set()
    while not stop.wait(4):
        try:
            with _lock:
                text = kakao_lib.read_room(room, open_if_needed=False)
        except Exception:
            continue
        lines = text.splitlines()
        new = [LINE_RE.match(l).groups() for l in lines if l not in seen and LINE_RE.match(l)]
        seen = set(lines)
        incoming = [g for g in new if g[0] != MY_NAME]
        if not incoming:
            continue
        try:
            reply = _generate(text)
        except Exception as e:
            print("[auto] LLM 실패:", e, flush=True)
            continue
        if reply:
            try:
                send(room, reply)
            except Exception as e:
                print("[auto] 전송 실패:", e, flush=True)


def auto_start(room):
    if room in _auto:
        return
    # 자동응답 켤 때 방이 닫혀있으면 백그라운드로 연다(최소화+포커스 복귀)
    with _lock:
        try:
            _ensure_open_bg(room)
        finally:
            _release_input()
    stop = threading.Event()
    _auto[room] = stop
    threading.Thread(target=_auto_loop, args=(room, stop), daemon=True).start()


def auto_stop(room):
    stop = _auto.pop(room, None)
    if stop:
        stop.set()


def auto_on(room):
    return room in _auto


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "max-age=3600")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_media(self, q):
        import urllib.request
        kind = q.get("kind", [""])[0]
        try:
            if kind == "emoticon":
                name = os.path.basename(q.get("path", [""])[0])
                cd = kakdb._find_chat_data() if kakdb else None
                if cd:
                    for sub in ("e", os.path.join("e", "small"), "x2.0"):
                        fp = os.path.join(os.path.dirname(cd), "DigitalItem", sub, name)
                        if os.path.exists(fp):
                            with open(fp, "rb") as f:
                                self._send_bytes(200, f.read(), "image/png")
                            return
                self._send(404, {"error": "emoticon not cached"})
                return
            url = q.get("url", [""])[0]
            if not url:
                self._send(404, {"error": "no url"})
                return
            req = urllib.request.Request(url, headers={
                "User-Agent": "KakaoTalk", "Referer": "https://talk.kakaocdn.net/"})
            r = urllib.request.urlopen(req, timeout=15)
            self._send_bytes(200, r.read(), r.headers.get("Content-Type", "image/jpeg"))
        except Exception as e:
            self._send(502, {"error": str(e)})

    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/rooms":
                rooms = None
                if kakdb is not None:
                    try:
                        rooms = kakdb.get_all_rooms()   # 전체 방(로컬 DB 복호화)
                    except Exception as e:
                        print("[kakdb] 실패, 폴백:", e, flush=True)
                self._send(200, {"rooms": rooms if rooms else list_rooms()})
            elif u.path == "/read":
                room = q.get("room", [""])[0]
                allow_open = q.get("openok", ["0"])[0] == "1"
                db = None
                if kakdb is not None:
                    try:
                        db = kakdb.read_messages(room)   # DB 직접 읽기(팝업 없이 즉시)
                    except Exception as e:
                        print("[kakdb read] 실패:", e, flush=True)
                if db is not None:
                    self._send(200, {"messages": db, "open": True, "source": "db"})
                else:
                    msgs = read_messages(room, allow_open=allow_open)  # UI 폴백(DB에 없는 방만)
                    for m in msgs:
                        m["mine"] = (m.get("sender") == MY_NAME)
                    self._send(200, {"messages": msgs, "open": bool(win32gui.FindWindow(None, room))})
            elif u.path == "/media":
                self._serve_media(q)
            elif u.path == "/auto/status":
                self._send(200, {"on": auto_on(q.get("room", [""])[0])})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) if n else b""
        if u.path == "/sendmedia":        # 바이너리 본문 = 파일, 쿼리에 room/name
            q = parse_qs(u.query)
            try:
                send_media_bytes(q.get("room", [""])[0], q.get("name", ["file"])[0], raw)
                self._send(200, {"ok": True})
            except Exception as e:
                self._send(500, {"error": str(e)})
            return
        try:
            data = json.loads(raw or b"{}")
        except Exception:
            data = {}
        try:
            if self.path == "/send":
                send(data["room"], data["text"])
                self._send(200, {"ok": True})
            elif self.path == "/rooms/add":
                r = data["room"].strip()
                cfg = _load_config()
                if r and r not in cfg:
                    cfg.append(r)
                    _save_config(cfg)
                self._send(200, {"rooms": list_rooms()})
            elif self.path == "/rooms/remove":
                _save_config([x for x in _load_config() if x != data["room"]])
                self._send(200, {"rooms": list_rooms()})
            elif self.path == "/markread":
                # 읽음 동기화 ON일 때만 호출됨: 방을 백그라운드로 열어 폰까지 읽음 처리
                with _lock:
                    try:
                        _ensure_open_bg(data["room"])
                    finally:
                        _release_input()
                self._send(200, {"ok": True})
            elif self.path == "/auto/start":
                auto_start(data["room"])
                self._send(200, {"on": True})
            elif self.path == "/auto/stop":
                auto_stop(data["room"])
                self._send(200, {"on": False})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def log_message(self, *args):
        pass  # 콘솔 조용히


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    threading.Thread(target=_poll_notifications, daemon=True).start()
    print(f"[kakao-server] listening on 127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
