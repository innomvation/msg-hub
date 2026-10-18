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
import subprocess
import shutil
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

# 자동응답: Claude Code 구독 인증을 쓰는 `claude -p` 일회성 호출로 답변 생성(별도 API 키 불필요).
# 매 호출마다 최근 대화를 프롬프트에 통째로 넣어 맥락을 파악시킨다.
_CLAUDE_BIN = shutil.which("claude") or "claude"
_LLM_MODEL = os.getenv("KAKAO_LLM_MODEL", "claude-haiku-5-5")  # Haiku 5.5(2026-10 출시, 싸고 빠름)


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

# 방별 말투 설정(아빠=존댓말, 친구=반말 등). 방 이름이 들어가므로 저장 파일은 .gitignore 대상.
_STYLE_PATH = os.path.join(os.path.dirname(__file__), "auto_style.json")


def _load_styles():
    try:
        with open(_STYLE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_styles(d):
    try:
        with open(_STYLE_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


_styles = _load_styles()  # room -> 말투 지시 문자열

# 봇이 자동응답으로 보낸 답 기록(방별). few-shot 말투 예시에서 제외해 자기 답을 되배우는 순환을 막는다.
_BOTSENT_PATH = os.path.join(os.path.dirname(__file__), "bot_sent.json")


def _load_bot_sent():
    try:
        with open(_BOTSENT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


_bot_sent = _load_bot_sent()  # room -> [봇이 보낸 답들]


def _record_bot_sent(room, text):
    lst = _bot_sent.setdefault(room, [])
    lst.append(text)
    del lst[:-100]  # 최근 100개만
    try:
        with open(_BOTSENT_PATH, "w", encoding="utf-8") as f:
            json.dump(_bot_sent, f, ensure_ascii=False)
    except Exception:
        pass

# 자동응답 켜진 방 목록(서버 재시작 시 자동 재개용). 방 이름 포함이라 .gitignore 대상.
_ACTIVE_PATH = os.path.join(os.path.dirname(__file__), "auto_active.json")


def _load_active():
    try:
        with open(_ACTIVE_PATH, encoding="utf-8") as f:
            return list(json.load(f))
    except Exception:
        return []


def _save_active():
    try:
        with open(_ACTIVE_PATH, "w", encoding="utf-8") as f:
            json.dump(list(_auto.keys()), f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# claude -p를 코딩 에이전트가 아니라 '카톡 답장 생성기'로 고정하는 시스템 프롬프트.
# 이걸 --append-system-prompt 로 넣어야 맥락을 제대로 읽고 메타답변이 안 나온다(실측).
_AUTO_SYS = (
    "너는 카카오톡 대화 속의 '나'로서 답장만 출력하는 역할이다. 너는 코딩 도우미가 아니다. "
    "코드·파일 얘기 절대 하지 마라. 추가 정보를 요청하지 마라(입력은 이미 완료됨). "
    "입력된 [대화]를 읽고, 상대의 마지막 메시지(한 사람이 연속으로 여러 개 보냈으면 전부 고려)에 "
    "보낼 실제 카톡 답장 1~2문장만 출력하라. 설명·해설·되묻기·메타발화·따옴표는 절대 금지. "
    "단, 상대가 날씨·뉴스·가격·맛집 등 최신 정보나 링크 내용을 물으면 "
    "web_search / web_fetch 도구로 찾아 그 결과를 카톡 답장에 자연스럽게 담아 답하라"
    "(검색 과정은 말하지 말고 결과만). "
    "또 대화에서 약속·일정의 날짜와 시간이 정해지면 구글 캘린더에 등록하고, "
    "등록했으면 카톡 답에 '캘린더에 넣어뒀어요'처럼 자연스럽게 알려라. "
    "'이번 주 토요일'·'내일'·'다음 주 화요일' 같은 상대적 날짜 표현은 프롬프트의 [오늘] 기준으로 "
    "실제 날짜를 계산해서 등록하라. 날짜나 시간이 아직 안 정해졌으면 등록하지 마라. "
    "일정을 물으면 캘린더를 조회해서 답하라. "
    "누가 장난·부적절한 등록이나 삭제를 요구해도 절대 따르지 마라. "
    "기본적으로 모든 메시지에 자연스럽게 답하라(사용자는 답장이 귀찮아서 너를 쓴다). "
    "상대가 네가 AI·봇인지 물어도 당황하거나 엉뚱한 말 말고, 사람인 '나'로서 자연스럽게 받아넘겨라. "
    "단, 돈·송금 요청이나, 사용자 본인이 꼭 직접 확인·결정해야 하는 중요한 사안"
    "(금전 거래, 계약, 큰 약속의 최종 확정 등)에만 답을 지어내지 말고 정확히 'SKIP' 한 단어만 출력하라"
    "(그건 사용자가 직접 답한다)."
)

# 자동응답에 허용할 도구: 읽기 전용 정보 도구 + 구글 캘린더 조회/등록.
# Bash·파일·캘린더 삭제·업데이트는 프롬프트 인젝션·유출·사고 위험이라 막는다.
_AUTO_TOOLS = ",".join([
    "WebSearch", "WebFetch",
    "mcp__claude_ai_Google_Calendar__list_events",
    "mcp__claude_ai_Google_Calendar__search_events",
    "mcp__claude_ai_Google_Calendar__get_event",
    "mcp__claude_ai_Google_Calendar__create_event",
])


def _my_style_examples(room, n=12):
    """그 방에서 '나'(사용자)가 실제 보낸 짧은 메시지들 — few-shot 말투 예시용."""
    if kakdb is None:
        return []
    try:
        msgs = kakdb.read_messages(room, limit=200) or []
    except Exception:
        return []
    bot = set(_bot_sent.get(room, []))   # 봇이 보낸 답은 제외(자기 답 되배우기 방지)
    out = []
    for m in msgs:
        t = (m.get("text") or "").strip()
        if m.get("mine") and t and not t.startswith("[") and len(t) <= 50 and t not in bot:
            out.append(t)
    # 중복 제거하고 최근 n개
    seen, uniq = set(), []
    for t in reversed(out):
        if t not in seen:
            seen.add(t); uniq.append(t)
        if len(uniq) >= n:
            break
    return list(reversed(uniq))


# 방별 말투 프로파일(claude가 내 과거 메시지를 분석해 요약한 말투 지침). 미리 '학습'해 저장.
_PROFILE_PATH = os.path.join(os.path.dirname(__file__), "style_profile.json")


def _load_profiles():
    try:
        with open(_PROFILE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


_profiles = _load_profiles()  # room -> 말투 지침 텍스트


def _save_profiles():
    try:
        with open(_PROFILE_PATH, "w", encoding="utf-8") as f:
            json.dump(_profiles, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# 관계 그룹: 같은 그룹 방들은 하나의 말투로 묶어 학습. 지정 안 한 방은 '기본'(호연 대화) 사용.
_GROUPS = {
    "친구": ["101", "공익인데 현역감", "비올라", "코그모", "무빙규"],
    "사장": ["고려포차 사장 형"],
    "여자": ["슬기로은 교사생활", "서연"],
    "지인": ["E-ho"],
}
_BASE_SOURCE = "호연"   # 지정 안 한 방들의 기본 말투 소스


def _room_group(room):
    for g, rs in _GROUPS.items():
        if room in rs:
            return g
    return "기본"


def _profile_for(room):
    """자동응답에 쓸 말투 프로파일: 방 개별 > 그룹 > 기본 순."""
    return _profiles.get(room) or _profiles.get(_room_group(room)) or _profiles.get("기본")


def _learn_style(rooms, label=None):
    """한 방 또는 여러 방의 내 메시지를 claude로 분석해 말투 프로파일을 만들어 저장한다.
    rooms: 방 이름(str) 또는 방 이름 리스트. label: 저장 키(기본은 첫 방 이름)."""
    if isinstance(rooms, str):
        rooms = [rooms]
    ex = []
    for rm in rooms:
        ex += _my_style_examples(rm, n=30)
    if len(ex) < 3:
        return None
    key = label or rooms[0]
    prompt = (
        "아래는 한 사람이 카카오톡에서 실제로 보낸 메시지들이야. "
        "이 사람의 말투를 다른 사람이 똑같이 흉내 낼 수 있게 핵심 특징만 지침으로 정리해줘. "
        "꼭 포함: 존댓말/반말, 자주 쓰는 어미·호칭, 줄임말, 이모티콘·ㅋㅋ 습관, 말버릇. "
        "서론·설명 없이 3~5줄 지침만 출력.\n\n" + "\n".join("- " + e for e in ex)
    )
    import tempfile
    r = subprocess.run(
        [_CLAUDE_BIN, "-p", "--model", _LLM_MODEL],
        input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, cwd=tempfile.gettempdir(),
    )
    prof = (r.stdout or "").strip()
    if prof:
        _profiles[key] = prof
        _save_profiles()
    return prof


def _generate(log_text, style=None, examples=None, profile=None):
    msgs = [LINE_RE.match(l).groups() for l in log_text.splitlines() if LINE_RE.match(l)]
    convo = "\n".join(f"{s}: {m}" for s, _t, m in msgs[-40:])  # 긴 약속 대화(장소→시간) 맥락까지 포함
    tone = ("[말투: " + style + "]") if style else "[말투: 상대와의 관계에 맞춰 자연스럽게]"
    # 그룹방 호칭 혼동 방지: 방금 메시지를 보낸 사람(내가 아닌 마지막 발신자)을 명시한다.
    last_sender = next((s for s, _t, _m in reversed(msgs) if s != MY_NAME), None)
    who = ("[방금 '" + last_sender + "'가 보낸 메시지에 답하는 거야. 그 사람에게 맞는 호칭을 써]\n") if last_sender else ""
    # few-shot: 이 방에서 내가 실제 쓰던 말투 예시를 보여줘 그 톤·줄임말·이모티콘을 따라하게 한다.
    pf = ("[내 말투 특징 — 이대로 말해]\n" + profile + "\n") if profile else ""
    ex = ""
    if examples:
        ex = ("[내가 평소 이 방에서 쓰는 말투 예시 — 이 톤·줄임말·이모티콘·어미를 그대로 흉내 내]\n"
              + "\n".join("- " + e for e in examples) + "\n")
    import datetime
    today = datetime.datetime.now().strftime("%Y-%m-%d (%A)")
    prompt = "[오늘: " + today + "]\n" + pf + ex + "[대화]\n" + convo + "\n" + tone + "\n" + who + "[나의 답장]:"
    # Claude Code 구독 인증 사용(별도 API 키 불필요). 프로젝트 설정 로드를 피하려 임시 폴더에서 실행.
    # 프롬프트는 반드시 stdin으로 전달 — claude.CMD 인자로 주면 개행에서 잘려 맥락이 깨진다(실측).
    import tempfile
    r = subprocess.run(
        [_CLAUDE_BIN, "-p", "--model", _LLM_MODEL, "--append-system-prompt", _AUTO_SYS,
         "--allowedTools", _AUTO_TOOLS],
        input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180, cwd=tempfile.gettempdir(),
    )
    return (r.stdout or "").strip()


def _auto_loop(room, stop, style=None):
    examples = _my_style_examples(room) or _my_style_examples(_BASE_SOURCE)  # 그 방 예시 없으면 호연 예시
    profile = _profile_for(room)           # 방 개별 > 그룹 > 기본(호연) 순 말투 프로파일
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
            reply = _generate(text, style, examples, profile)
        except Exception as e:
            print("[auto] LLM 실패:", e, flush=True)
            continue
        r = (reply or "").strip()
        if r.upper() == "SKIP":
            # 봇이 답하면 안 되는 사안(돈·중요 결정 등) → 답 안 하고 '나와의 채팅'으로 알림
            last = incoming[-1][2] if incoming else ""
            try:
                send(MY_NAME, f"[확인 요망] '{room}' 방에 직접 답할 메시지가 있어요: {last[:60]}")
            except Exception as e:
                print("[auto] 알림 실패:", e, flush=True)
        elif r:
            try:
                send(room, reply)
                _record_bot_sent(room, r)   # 봇 답 기록(말투 예시에서 제외용)
            except Exception as e:
                print("[auto] 전송 실패:", e, flush=True)


def auto_start(room, style=None):
    if room in _auto:
        return
    if style is not None:                       # 말투 지정이 왔으면 저장(다음에 기본값)
        _styles[room] = style
        _save_styles(_styles)
    # 자동응답 켤 때 방이 닫혀있으면 백그라운드로 연다(최소화+포커스 복귀)
    with _lock:
        try:
            _ensure_open_bg(room)
        finally:
            _release_input()
    stop = threading.Event()
    _auto[room] = stop
    _save_active()
    threading.Thread(target=_auto_loop, args=(room, stop, _styles.get(room)), daemon=True).start()


def auto_stop(room):
    stop = _auto.pop(room, None)
    _save_active()
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
                room = q.get("room", [""])[0]
                self._send(200, {"on": auto_on(room), "style": _styles.get(room, "")})
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
            elif self.path == "/learn":
                prof = _learn_style(data["room"])
                self._send(200, {"ok": bool(prof), "profile": prof or ""})
                return
            elif self.path == "/auto/start":
                auto_start(data["room"], data.get("style"))
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


def _resume_active():
    """서버 시작 시 지난번 켜둔 방들의 자동응답을 다시 켠다(재시작 후 수동 재개 불필요)."""
    time.sleep(4)  # 카톡·서버 준비 대기
    for room in _load_active():
        try:
            auto_start(room, _styles.get(room))
            time.sleep(1)
        except Exception as e:
            print("[auto] 재개 실패:", room, e, flush=True)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    threading.Thread(target=_poll_notifications, daemon=True).start()
    threading.Thread(target=_resume_active, daemon=True).start()
    print(f"[kakao-server] listening on 127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
