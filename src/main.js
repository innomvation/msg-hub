const { app, BrowserWindow, webContents } = require("electron");
const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");

// 디스코드 로그인 유지: 로그인된 토큰을 저장해두고, 로그아웃되면 재주입.
// 디스코드가 토큰 탈취 방지로 localStorage를 지워서, 같은 출처 iframe으로 접근한다.
function setupDiscordAuth(win) {
  const tokenFile = path.join(app.getPath("userData"), "discord_token.txt");
  const READ = "(function(){try{var f=document.createElement('iframe');document.body.appendChild(f);" +
    "var t=f.contentWindow.localStorage.getItem('token');f.remove();return t;}catch(e){return null;}})()";
  const write = (t) => "(function(){try{var f=document.createElement('iframe');document.body.appendChild(f);" +
    "f.contentWindow.localStorage.setItem('token'," + JSON.stringify(t) + ");f.remove();return true;}catch(e){return false;}})()";

  win.webContents.on("did-attach-webview", (e, guest) => {
    // 모든 webview(디코·인스타): 마우스 뒤로가기 버튼 → 웹 뒤로(history.back)
    const injectBack = () => guest.executeJavaScript(
      "window.addEventListener('mouseup',function(e){if(e.button===3){e.preventDefault();history.back();}},true);"
    ).catch(() => {});
    guest.on("dom-ready", injectBack);

    const isDiscord = () => { try { return guest.getURL().includes("discord.com"); } catch (_) { return false; } };
    const save = async () => {
      if (!isDiscord()) return;
      try {
        const t = await guest.executeJavaScript(READ);
        if (t && t.length > 20) fs.writeFileSync(tokenFile, t, "utf8");
      } catch (_) {}
    };
    guest.on("dom-ready", async () => {
      if (!isDiscord()) return;
      try {
        const cur = await guest.executeJavaScript(READ);
        if (cur && cur.length > 20) {
          fs.writeFileSync(tokenFile, cur, "utf8");           // 로그인돼 있으면 저장
        } else if (fs.existsSync(tokenFile)) {
          const saved = fs.readFileSync(tokenFile, "utf8");
          if (saved && saved.length > 20) {
            await guest.executeJavaScript(write(saved));       // 로그아웃돼 있으면 재주입
            guest.reload();
          }
        }
      } catch (_) {}
    });
    setInterval(save, 30000);                                   // 최신 토큰 주기적 저장
  });
}

const KAKAO_PORT = 8787;
let kakaoServer = null;

function startKakaoServer() {
  // 카톡 패널 백엔드(kakao_lib 래퍼)를 같이 띄운다
  kakaoServer = spawn(
    "python",
    [path.join(__dirname, "kakao", "server.py"), String(KAKAO_PORT)],
    { stdio: "ignore" }
  );
  kakaoServer.on("error", (e) => console.error("[kakao-server] 실행 실패:", e.message));
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1200,
    height: 800,
    title: "메신저 허브",
    webPreferences: {
      webviewTag: true, // 디코·인스타 공식 웹을 창 안에 띄우기 위함
    },
  });
  win.setMenuBarVisibility(false);
  // Ctrl+R / F5 로 새로고침 (UI 수정 즉시 반영용)
  win.webContents.on("before-input-event", (e, input) => {
    if (input.type !== "keyDown") return;
    if (input.key === "F5" || (input.control && input.key.toLowerCase() === "r")) win.reload();
  });
  win.loadFile(path.join(__dirname, "renderer", "index.html"));
  setupDiscordAuth(win);

  // 마우스 뒤로가기 버튼: 디코·인스타(포커스된 webview)는 웹 뒤로, 그 외(카톡)는 목록으로
  win.on("app-command", (e, cmd) => {
    if (cmd !== "browser-backward") return;
    const fw = webContents.getFocusedWebContents();
    if (fw && fw.id !== win.webContents.id) {
      if (fw.canGoBack()) fw.goBack();
    } else {
      win.webContents.executeJavaScript("window.__backToList && window.__backToList()").catch(() => {});
    }
  });
}

app.whenReady().then(() => {
  startKakaoServer();
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("will-quit", () => {
  if (kakaoServer) kakaoServer.kill();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
