// 아이콘을 누르면 Side Panel 이 열리게 합니다.
// content script 가 보내는 SHORTS_CHANGED 는 Side Panel 이 직접 받으므로
// 여기서 따로 중계하지 않습니다.
chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(console.error);
});

// --- 저장 창 뒷정리 --------------------------------------------------------
//
// 저장은 **별도 창**에서 돕니다(사용자가 그동안 다른 쇼츠를 볼 수 있게). 화면이 넓으면
// 원래 창을 좁혀 자리를 비워주는데, 그 크기를 반드시 되돌려야 합니다.
//
// ⚠ 되돌리는 일을 사이드패널에 두면 안 됩니다. 사이드패널은 사용자가 닫으면 그냥
//   사라지고, 그러면 복구 코드도 같이 죽어 **사용자 창이 좁아진 채로 남습니다.**
//   그래서 상태를 chrome.storage 에 적고 여기(서비스 워커)에서 되돌립니다.
//   서비스 워커도 잠들 수 있지만, 아래 이벤트 중 하나만 깨워도 복구가 돌아갑니다.

const RESTORE_KEY = "pendingWindowRestore";

async function restorePending() {
  const { [RESTORE_KEY]: saved } = await chrome.storage.local.get(RESTORE_KEY);
  if (!saved) return;
  await chrome.storage.local.remove(RESTORE_KEY); // 먼저 지웁니다 — 두 번 복구하지 않게
  try {
    await chrome.windows.update(saved.windowId, {
      state: saved.state === "maximized" ? "maximized" : "normal",
    });
    if (saved.state !== "maximized") {
      await chrome.windows.update(saved.windowId, {
        left: saved.left, top: saved.top, width: saved.width, height: saved.height,
      });
    }
  } catch (_) {
    // 창이 이미 닫혔으면 할 일이 없습니다.
  }
  if (saved.captureWindowId) {
    try {
      await chrome.windows.remove(saved.captureWindowId);
    } catch (_) {}
  }
}

// 저장 창이 닫히면(정상 종료든, 사용자가 손으로 닫든) 원래 크기로 되돌립니다.
chrome.windows.onRemoved.addListener(async (windowId) => {
  const { [RESTORE_KEY]: saved } = await chrome.storage.local.get(RESTORE_KEY);
  if (saved && saved.captureWindowId === windowId) {
    await chrome.storage.local.set({ [RESTORE_KEY]: { ...saved, captureWindowId: null } });
    await restorePending();
  }
});

// 브라우저를 껐다 켰거나 확장이 다시 올라온 경우에도 남은 복구를 처리합니다.
chrome.runtime.onStartup.addListener(restorePending);
chrome.runtime.onInstalled.addListener(restorePending);
restorePending();

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  // 사이드패널이 창을 좁히기 **전에** 원래 크기를 맡겨 둡니다.
  if (message?.type === "REMEMBER_WINDOW") {
    chrome.storage.local.set({ [RESTORE_KEY]: message.state }).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (message?.type === "RESTORE_WINDOW") {
    restorePending().then(() => sendResponse({ ok: true }));
    return true;
  }
  return false;
});
