// 아이콘을 누르면 Side Panel 이 열리게 합니다.
// content script 가 보내는 SHORTS_CHANGED 는 Side Panel 이 직접 받으므로
// 여기서 따로 중계하지 않습니다.
chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(console.error);
});
