"""카카오톡 창(메인 + 튀어나온 채팅방)을 메신저 허브의 3번째 칸에 끼워넣고 동기화한다.

- 허브 창('메신저 허브')과 카톡 메인창('카카오톡')을 찾아 카톡을 허브의 자식 창으로 만든다.
- 카톡은 채팅을 열면 '채팅방'을 별도 top-level 창으로 띄우는데, 그 창도 같은 칸에 끼워
  메인 목록 위에 겹쳐 올린다(가장 최근 연 채팅방이 맨 위). 채팅방을 닫으면 목록이 다시 보인다.
- 허브가 움직이거나 크기가 바뀌면 끼운 창들이 모두 칸에 맞춰 따라간다.
- 허브가 최소화되면 숨기고, 닫히면 끼운 창을 모두 원래의 평범한 창으로 되돌리고 종료한다.

Electron main 이 이 스크립트를 detached 로 띄운다. 허브가 사라지면 스스로 복구 후 종료한다.
"""
import time
import ctypes
import win32gui as g
import win32con as c
import win32process as wp

_user32 = ctypes.windll.user32


def _attach_input(hub, win):
    """허브 스레드와 카톡 창 스레드의 입력 큐를 연결해, 끼운 창이 클릭/키 입력을
    받을 수 있게 한다(크로스-프로세스 임베드의 입력 문제 해결). 양방향으로 건다."""
    ht, _ = wp.GetWindowThreadProcessId(hub)
    wt, _ = wp.GetWindowThreadProcessId(win)
    if ht and wt and ht != wt:
        _user32.AttachThreadInput(ht, wt, True)
        _user32.AttachThreadInput(wt, ht, True)

HUB_TITLE = "메신저 허브"
MAIN_TITLE = "카카오톡"
COLS = 3
HEAD = 38          # 칸 상단 헤더 높이(px)
WS_POPUP = 0x80000000


def find_hub():
    res = [0]

    def cb(h, _):
        if g.IsWindowVisible(h) and g.GetWindowText(h) == HUB_TITLE:
            res[0] = h

    g.EnumWindows(cb, None)
    return res[0]


def top_kakao_windows():
    """아직 안 끼운(최상위) 카톡 창들을 찾는다. (메인 핸들, [채팅방 핸들...])."""
    main = [0]
    chats = []

    def cb(h, _):
        if not g.IsWindowVisible(h):
            return
        if g.GetClassName(h) != "EVA_Window_Dblclk":
            return
        t = g.GetWindowText(h)
        if not t:
            return
        if t == MAIN_TITLE:
            main[0] = h
        else:
            chats.append(h)

    g.EnumWindows(cb, None)
    return main[0], chats


def col_rect(hub):
    l, t, r, b = g.GetClientRect(hub)
    cw, ch = r - l, b - t
    x = cw * (COLS - 1) // COLS       # 마지막 칸 시작 x
    return x, HEAD, cw - x, ch - HEAD


def embed(win, hub, styles):
    if win not in styles:
        styles[win] = g.GetWindowLong(win, c.GWL_STYLE)
    st = (styles[win] & ~c.WS_CAPTION & ~c.WS_THICKFRAME & ~WS_POPUP) | c.WS_CHILD | c.WS_VISIBLE
    g.SetWindowLong(win, c.GWL_STYLE, st)
    g.SetParent(win, hub)
    _attach_input(hub, win)


def restore_one(win, styles):
    try:
        g.SetParent(win, 0)
        g.SetWindowLong(win, c.GWL_STYLE, styles.get(win, c.WS_OVERLAPPEDWINDOW | c.WS_VISIBLE))
        g.SetWindowPos(win, 0, 200, 120, 520, 780, 0x0020)  # SWP_FRAMECHANGED
        g.ShowWindow(win, c.SW_SHOWNORMAL)
    except Exception:
        pass


def main():
    styles = {}        # hwnd -> 원래 스타일
    embedded = []      # 순서대로: [메인, 채팅방1, 채팅방2 ...] (뒤일수록 z-위)
    seen_hub = False
    start = time.time()
    last_rect = None
    try:
        while True:
            hub = find_hub()
            if not hub:
                if seen_hub or time.time() - start > 20:
                    break
                time.sleep(0.3)
                continue
            seen_hub = True

            # 아직 안 끼운(최상위) 카톡 창들을 끼운다
            main_top, chats_top = top_kakao_windows()
            changed = False
            if main_top and main_top not in embedded:
                embed(main_top, hub, styles)
                embedded.insert(0, main_top)   # 메인은 맨 아래
                changed = True
            for w in chats_top:
                if w not in embedded:
                    embed(w, hub, styles)
                    embedded.append(w)         # 새 채팅방은 맨 위로
                    changed = True

            # 닫힌 창 제거
            alive = [w for w in embedded if g.IsWindow(w)]
            if len(alive) != len(embedded):
                changed = True
                embedded = alive

            if not embedded:
                time.sleep(0.4)
                continue

            if g.IsIconic(hub):               # 허브 최소화 → 다 숨김
                for w in embedded:
                    g.ShowWindow(w, c.SW_HIDE)
                last_rect = None
                time.sleep(0.3)
                continue

            rect = col_rect(hub)
            if changed or rect != last_rect:
                for w in embedded:
                    g.ShowWindow(w, c.SW_SHOW)
                    g.MoveWindow(w, rect[0], rect[1], rect[2], rect[3], True)
                for w in embedded:            # 순서대로 top → 마지막(최근 채팅)이 맨 위
                    g.SetWindowPos(w, 0, 0, 0, 0, 0, 0x1 | 0x2 | 0x40)
                last_rect = rect
            time.sleep(0.15)
    finally:
        for w in embedded:
            if g.IsWindow(w):
                restore_one(w, styles)


if __name__ == "__main__":
    main()
