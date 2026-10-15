const API_BASE = "http://localhost:8000";
const POLL_MS = 3000;

let userId = null;
let currentInfo = null;
let playlists = [];
let selectedPlaylist = "";
let pollTimer = null;
let serverHealth = null;

// --- 기본 도구 -------------------------------------------------------------

const $ = (id) => document.getElementById(id);

function fmtTime(sec) {
  const s = Math.max(0, Math.round(sec || 0));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("err", isError);
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 3500);
}

async function getUserId() {
  if (userId) return userId;
  const stored = await chrome.storage.local.get("userId");
  if (stored.userId) {
    userId = stored.userId;
  } else {
    userId = crypto.randomUUID();
    await chrome.storage.local.set({ userId });
  }
  return userId;
}

async function api(path, options = {}) {
  const uid = await getUserId();
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-User-Id": uid,
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      detail = (await response.json()).detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

// --- 지금 보고 있는 쇼츠 ---------------------------------------------------

async function activeYoutubeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url || !/^https:\/\/(www|m)\.youtube\.com\//.test(tab.url)) return null;
  return tab;
}

async function askContentScript(tabId, message) {
  try {
    return await chrome.tabs.sendMessage(tabId, message);
  } catch (_) {
    // 확장을 나중에 켠 경우 content script 가 아직 없을 수 있습니다.
    try {
      await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
      return await chrome.tabs.sendMessage(tabId, message);
    } catch (err) {
      return null;
    }
  }
}

async function refreshCurrentVideo() {
  const tab = await activeYoutubeTab();
  if (!tab) {
    setCurrentInfo(null, "유튜브 탭이 아닙니다");
    return;
  }
  const info = await askContentScript(tab.id, { type: "GET_VIDEO_INFO" });
  setCurrentInfo(info);
}

function setCurrentInfo(info, reason = "") {
  currentInfo = info;
  const titleEl = $("current-title");
  const subEl = $("current-sub");
  const saveBtn = $("save-btn");

  if (!info || !info.videoId) {
    titleEl.textContent = reason || "유튜브 쇼츠를 열어주세요";
    subEl.textContent = "";
    saveBtn.disabled = true;
    return;
  }
  titleEl.textContent = info.title || info.videoId;
  subEl.textContent = `${info.channel || "채널 정보 없음"} · ${info.videoId}${
    info.isShorts ? "" : " (쇼츠 아님)"
  }`;
  saveBtn.disabled = false;
}

// --- 저장 -----------------------------------------------------------------

// 단계 2 · 추출은 실시간 재생이라 영상 길이만큼 걸립니다.
// 저장은 별도 창에서 돌아갑니다. 사용자는 그동안 다른 쇼츠를 봐도 됩니다.
function showCapture(visible, ratio = 0, message = "") {
  const box = $("capture");
  box.classList.toggle("hidden", !visible);
  $("capture-fill").style.width = `${Math.round(ratio * 100)}%`;
  if (message) $("capture-msg").textContent = message;
}

async function saveCurrent() {
  // 리스너가 못 잡은 경우에도 엉뚱한 영상을 저장하지 않도록, 보내기 직전에 다시 읽습니다.
  await refreshCurrentVideo();
  if (!currentInfo?.videoId) return;
  const videoId = currentInfo.videoId;
  const btn = $("save-btn");
  btn.disabled = true;

  try {
    const accepted = await api("/api/videos", {
      method: "POST",
      body: JSON.stringify({
        video_id: videoId,
        title: currentInfo.title,
        channel: currentInfo.channel,
        duration: currentInfo.duration,
        playlist_id: selectedPlaylist || null,
      }),
    });

    if (!accepted.needs_capture) {
      toast("이미 저장된 영상입니다.");
      await loadLibrary();
      return;
    }

    await loadLibrary();

    showCapture(true, 0, "저장 창을 여는 중입니다. 이 창에서는 다른 쇼츠를 보셔도 됩니다.");
    const capture = await openCaptureWindow(videoId);
    let result;
    try {
      result = await askContentScript(capture.tabId, {
        type: "CAPTURE_START",
        videoId,
        settings: accepted.capture,
        apiBase: API_BASE,
        userId: await getUserId(),
      });
    } finally {
      await closeCaptureWindow();
    }
    showCapture(false);
    if (result?.diag) console.log("[보관함] 캡처 진단", result.diag);

    if (!result?.ok) {
      const reason = result?.reason || "알 수 없는 오류";
      toast(`추출 실패: ${reason}`, true);
      await loadLibrary();
      return;
    }
    toast(`화면 ${result.frames}장 전송 완료. 분석을 시작합니다.`);
    await loadLibrary();
    startPolling();
  } catch (err) {
    showCapture(false);
    toast(`저장 실패: ${err.message}`, true);
  } finally {
    btn.disabled = false;
  }
}

// --- 저장 창 --------------------------------------------------------------
//
// 저장은 **별도 창**에서 돕니다. 같은 탭에서 하면 사용자가 다음 쇼츠로 넘기는 순간
// 유튜브가 <video> 요소를 갈아치워, 두 영상이 섞여 저장되거나 루프가 안 끝납니다.
// 창이 다르면 문서가 달라서 서로 건드릴 수 없습니다 — 격리가 공짜로 따라옵니다.
//
// 화면이 넓으면 원래 창을 좁혀 저장 창 자리를 비워 둡니다. Chrome 은 창이 **완전히
// 가려지면** 백그라운드 탭과 똑같이 타이머를 1초로 묶어서, 0.5초 간격 캡처가 절반으로
// 줄기 때문입니다. 자리를 비워두면 그 일이 안 생깁니다.
// 좁은 화면에서는 건드리지 않습니다 — 남는 폭이 너무 작아 쓸모가 없어집니다.

const CAPTURE_WIN_WIDTH = 480; // 유튜브가 저화질로 내려주지 않을 만큼은 돼야 합니다

// 저장 창이 가려져도 되는가 — **실측으로 답이 나왔습니다. 됩니다.**
//
//   틱 27회 중 26회가 가려진 상태(hiddenTicks 26/27)
//   틱 사이 최대 간격 501ms (목표 500ms) · 느려진 틱 0개
//   유튜브가 멈춘 횟수 0 · 화질 1280 유지 · 완주 97%
//
// Chrome 은 보통 가려진 창의 setInterval 을 1초로 묶는데 그 일이 없었습니다.
// 소리를 Web Audio 로 받고 있어서 Chrome 이 이 창을 "소리 내는 중" 으로 보기
// 때문으로 보입니다(확인된 원인은 아닙니다).
//
// 즉 **타일링(원래 창 좁히기)이 성능을 위해 필요하지는 않습니다.** 그래도 켜 둡니다 —
// 저장 창이 눈에 보이는 편이 "지금 뭐가 돌고 있다" 가 분명하고, 사용자가 실수로
// 저장 창을 닫아버리는 일도 줄기 때문입니다. 끄려면 아래를 false 로 하면 됩니다.
const TILE_WINDOWS = true;
const TILE_MIN_SCREEN = 1600; // 이보다 좁은 화면에서는 원래 창을 건드리지 않습니다

let captureWindowId = null;

async function openCaptureWindow(videoId) {
  const W = window.screen.availWidth;
  const H = window.screen.availHeight;
  const tile = TILE_WINDOWS && W >= TILE_MIN_SCREEN;

  if (tile) {
    const main = await chrome.windows.getCurrent();
    // 좁히기 **전에** 원래 크기를 background 에 맡깁니다. 사이드패널이 닫혀도
    // background 가 되돌려 줍니다.
    await chrome.runtime.sendMessage({
      type: "REMEMBER_WINDOW",
      state: {
        windowId: main.id, state: main.state,
        left: main.left, top: main.top, width: main.width, height: main.height,
      },
    });
    await chrome.windows.update(main.id, { state: "normal" });
    await chrome.windows.update(main.id, {
      left: 0, top: 0, width: W - CAPTURE_WIN_WIDTH, height: H,
    });
  }

  const win = await chrome.windows.create({
    url: `https://www.youtube.com/shorts/${videoId}`,
    type: "popup",
    focused: false, // 포커스를 뺏지 않습니다
    left: tile ? W - CAPTURE_WIN_WIDTH : Math.max(0, W - CAPTURE_WIN_WIDTH - 40),
    top: 0,
    width: CAPTURE_WIN_WIDTH,
    height: H,
  });
  captureWindowId = win.id;
  const tabId = win.tabs[0].id;

  if (tile) {
    const { pendingWindowRestore } = await chrome.storage.local.get("pendingWindowRestore");
    if (pendingWindowRestore) {
      await chrome.storage.local.set({
        pendingWindowRestore: { ...pendingWindowRestore, captureWindowId: win.id },
      });
    }
  }

  await waitForTab(tabId);
  await prepareCaptureTab(tabId);
  return { windowId: win.id, tabId };
}

function waitForTab(tabId) {
  return new Promise((resolve) => {
    const done = (id, info) => {
      if (id === tabId && info.status === "complete") {
        chrome.tabs.onUpdated.removeListener(done);
        setTimeout(resolve, 1200); // 유튜브 플레이어가 붙을 시간
      }
    };
    chrome.tabs.onUpdated.addListener(done);
    setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(done);
      resolve();
    }, 15000);
  });
}

/** 저장 창에만 적용합니다. 사용자가 보는 창은 건드리지 않습니다. */
async function prepareCaptureTab(tabId) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN", // 페이지 세계 — 유튜브 플레이어 객체에 닿으려면 필요합니다
      func: () => {
        // ① 창이 가려져도 유튜브가 스스로 멈추지 않게 "안 숨었다" 고 알려 줍니다.
        try {
          Object.defineProperty(document, "visibilityState", { get: () => "visible" });
          Object.defineProperty(document, "hidden", { get: () => false });
          document.addEventListener(
            "visibilitychange",
            (e) => e.stopImmediatePropagation(),
            true
          );
        } catch (_) {}
        // ② 창이 작아도 저화질로 내려주지 않게 화질을 고정합니다.
        //    자막 OCR 이 원본 해상도에 그대로 걸려 있어서 중요합니다.
        const fix = () => {
          const p = document.querySelector("#movie_player");
          if (p && p.setPlaybackQualityRange) {
            try {
              p.setPlaybackQualityRange("hd1080");
            } catch (_) {}
          }
        };
        fix();
        setTimeout(fix, 1500);
      },
    });
  } catch (err) {
    console.warn("[보관함] 저장 창 준비 실패(그대로 진행합니다):", err);
  }
}

async function closeCaptureWindow() {
  const id = captureWindowId;
  captureWindowId = null;
  if (id != null) {
    try {
      await chrome.windows.remove(id); // background 의 onRemoved 가 창 크기를 되돌립니다
    } catch (_) {}
  }
  await chrome.runtime.sendMessage({ type: "RESTORE_WINDOW" }).catch(() => {});
}

// --- 재생목록 --------------------------------------------------------------

async function loadPlaylists() {
  try {
    const data = await api("/api/playlists");
    playlists = data.playlists;
  } catch (_) {
    playlists = [];
  }
  const select = $("playlist-select");
  select.innerHTML = "";
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "전체 보관함";
  select.appendChild(all);
  for (const p of playlists) {
    const option = document.createElement("option");
    option.value = p.id;
    option.textContent = `${p.name} (${p.video_count})`;
    select.appendChild(option);
  }
  select.value = playlists.some((p) => p.id === selectedPlaylist) ? selectedPlaylist : "";
  selectedPlaylist = select.value;
}

async function createPlaylist() {
  const name = prompt("새 재생목록 이름");
  if (!name || !name.trim()) return;
  try {
    const created = await api("/api/playlists", {
      method: "POST",
      body: JSON.stringify({ name: name.trim() }),
    });
    selectedPlaylist = created.id;
    await loadPlaylists();
    await loadLibrary();
    toast(`'${created.name}' 재생목록을 만들었습니다.`);
  } catch (err) {
    toast(`재생목록 생성 실패: ${err.message}`, true);
  }
}

// --- 보관함 ---------------------------------------------------------------

const STAGE_NAMES = { asr: "음성", ocr: "자막", caption: "장면 설명", summary_llm: "요약" };

// 분석 상태 요약(analysis_health.summary) → 경고 문구. 분석이 완전하면 빈 문자열.
// 통째로 실패한 단계와 일부만 빠진 단계(예: 그리드 일부가 할당량으로 빠짐)를 함께 적습니다.
function healthWarning(health) {
  if (!health || health.complete !== false) return "";
  const label = (name) => STAGE_NAMES[name] || name;
  const names = [
    ...(health.failed_stages || []).map(label),
    ...(health.partial_stages || []).map((name) => `${label(name)}(일부)`),
  ].join(", ");
  const quota = health.quota_stages?.length ? " (Gemini 할당량 소진)" : "";
  return `⚠ 일부 분석 실패: ${names || "단계 정보 없음"}${quota}`;
}

function videoCard(video) {
  const card = document.createElement("div");
  card.className = "card";

  const img = document.createElement("img");
  img.src = video.thumbnail;
  img.alt = "";
  card.appendChild(img);

  const main = document.createElement("div");
  main.className = "card-main";

  const title = document.createElement("div");
  title.className = "card-title";
  title.textContent = video.title || video.video_id;
  main.appendChild(title);

  const sub = document.createElement("div");
  sub.className = "card-sub";
  sub.textContent = video.summary?.headline || video.channel || "";
  main.appendChild(sub);

  const status = document.createElement("div");
  status.className = `status ${video.status}`;
  status.textContent =
    video.status === "failed" ? `${video.status_label}: ${video.error}` : video.status_label;
  main.appendChild(status);

  // 분석은 끝났지만 일부 단계가 실패한 영상(예: 할당량 소진으로 장면 설명 없음).
  // 검색은 되지만 결과가 덜 정확할 수 있어 알려 줍니다. 할당량이 풀린 뒤 다시 저장하면 됩니다.
  const warning = video.status === "ready" ? healthWarning(video.summary?.analysis_health?.summary) : "";
  if (warning) {
    const warn = document.createElement("div");
    warn.className = "warn";
    warn.textContent = warning;
    main.appendChild(warn);
  }

  if (video.status !== "ready" && video.status !== "failed") {
    const bar = document.createElement("div");
    bar.className = "bar";
    const fill = document.createElement("i");
    fill.style.width = `${video.progress}%`;
    bar.appendChild(fill);
    main.appendChild(bar);
  }
  card.appendChild(main);

  const actions = document.createElement("div");
  actions.className = "card-actions";
  const del = document.createElement("button");
  del.className = "danger small";
  del.textContent = "삭제";
  del.addEventListener("click", async (event) => {
    event.stopPropagation();
    if (!confirm("이 영상을 보관함에서 지울까요?")) return;
    try {
      await api(`/api/videos/${video.video_id}`, { method: "DELETE" });
      toast("삭제했습니다.");
      await loadLibrary();
    } catch (err) {
      toast(`삭제 실패: ${err.message}`, true);
    }
  });
  actions.appendChild(del);
  card.appendChild(actions);

  card.addEventListener("click", () => openDetail(video.video_id));
  return card;
}

async function loadLibrary() {
  const list = $("library-list");
  try {
    const query = selectedPlaylist ? `?playlist_id=${encodeURIComponent(selectedPlaylist)}` : "";
    const data = await api(`/api/videos${query}`);
    list.innerHTML = "";
    data.videos.forEach((video) => list.appendChild(videoCard(video)));
    $("library-empty").classList.toggle("hidden", data.videos.length > 0);
  } catch (err) {
    list.innerHTML = "";
    $("library-empty").classList.remove("hidden");
    $("library-empty").textContent = `보관함을 불러오지 못했습니다: ${err.message}`;
  }
}

// --- 진행 상태 확인 (3초마다) ----------------------------------------------

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    try {
      const data = await api("/api/pending");
      await loadLibrary();
      if (data.pending_conditions?.length && compareVideoId) await loadConditions();
      if (data.pending.length === 0 && !data.pending_conditions?.length) {
        clearInterval(pollTimer);
        pollTimer = null;
        await loadPlaylists();
        if (compareVideoId) await loadConditions();
      }
    } catch (_) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }, POLL_MS);
}

// --- 시점 이동 (단계 10) ---------------------------------------------------

async function jumpTo(videoId, seekTime, fallbackUrl) {
  const tab = await activeYoutubeTab();
  if (tab) {
    let result = await askContentScript(tab.id, { type: "SEEK_TO", videoId, seconds: seekTime });
    if (result?.ok) {
      toast(`${fmtTime(seekTime)} 지점으로 이동했습니다.`);
      return;
    }
    if (result?.reason === "different_video") {
      // 다른 쇼츠를 보고 있으면 그 영상으로 옮긴 뒤 다시 시도합니다.
      await chrome.tabs.update(tab.id, { url: `https://www.youtube.com/shorts/${videoId}` });
      for (let i = 0; i < 6; i++) {
        await new Promise((r) => setTimeout(r, 700));
        result = await askContentScript(tab.id, { type: "SEEK_TO", videoId, seconds: seekTime });
        if (result?.ok) {
          toast(`${fmtTime(seekTime)} 지점으로 이동했습니다.`);
          return;
        }
      }
    }
  }
  // 폴백: 확실하게 동작하는 watch 주소로 새 탭을 엽니다.
  chrome.tabs.create({ url: fallbackUrl });
  toast("새 탭에서 해당 시점부터 재생합니다.");
}

// --- 검색 -----------------------------------------------------------------
//
// 검색 결과는 **영상 목록**을 먼저 보여주고, 영상을 펼쳐야 그 안 장면이 나옵니다.
// 예전에는 여러 영상의 장면이 평평하게 섞여 나와, 같은 제목이 반복되고 어느 영상에서
// 걸린 것인지 알기 어려웠습니다.
//
// 펼칠 때는 **그 영상만 다시 검색**합니다(같은 엔드포인트에 `video_id` 를 넣습니다).
// 전체 검색이 돌려주는 장면은 영상들이 top_k 개를 나눠 갖는 것이라, 영상 하나당
// 1~2개밖에 안 남아 드롭다운이 비어 보입니다.
//
// ⚠ 펼침 요청은 반드시 `with_answer: false` 입니다. AI 답변 생성은 Gemini 호출
//   1회이고 무료 티어가 하루 20회인데, 드롭다운에서는 그 답변을 쓰지도 않습니다.

const VIDEO_LIMIT = 10; // 검색 결과에 늘어놓을 영상 수
const SCENES_PER_VIDEO = 5; // 영상 하나를 펼쳤을 때 보여줄 장면 수

// 1위에 견줘 이 비율보다 낮은 영상은 접어 둡니다.
//
// ⚠ 이것은 **보기 정리이지 "관련 없음" 판정이 아닙니다.** 화면에 오는 score 는 후보
//   중 최고를 1.0 으로 맞춰 다시 계산한 값이라, 아무것도 안 맞는 질문에서도 1위는
//   만점이 됩니다(실측: "자동차 엔진 오일 교환하는 장면" → 1위 1.00).
//   진짜 "관련 장면 없음" 은 정규화 전 유사도로 따로 재야 하고, 그것이 멘토 자문
//   3번입니다(지금 성적 0/12). 이 줄을 그 해결책으로 착각하면 안 됩니다.
const RELATED_CUTOFF = 0.25;
// "영상 찾아줘" 는 영상을 고르는 질문이라 더 좁게 보여줍니다.
// ⚠ 이 값은 사례 하나에서 고른 판단값입니다(0.97 은 통과, 0.31 은 접힘).
//   평가셋으로 재본 값이 아니므로, 영상이 늘면 다시 봐야 합니다.
const VIDEO_INTENT_CUTOFF = 0.75;

function sceneCard(scene, showTitle = true) {
  const box = document.createElement("div");
  box.className = "scene";

  const head = document.createElement("div");
  head.className = "scene-head";

  // 저장할 때 뽑아둔 장면 사진 (단계 13)
  if (scene.frame_url) {
    const thumb = document.createElement("img");
    thumb.className = "scene-frame";
    thumb.src = `${API_BASE}${scene.frame_url}?u=${encodeURIComponent(userId)}`;
    thumb.alt = "";
    head.appendChild(thumb);
  }

  // 드롭다운 안에서는 제목이 바로 위 영상 줄과 같아서 뺍니다.
  // 비교 탭은 영상이 뒤섞여 나오므로 기본값(제목 표시)을 그대로 씁니다.
  const label = document.createElement("div");
  label.className = "card-title";
  label.textContent = showTitle ? scene.title || scene.video_id : "";
  head.appendChild(label);

  const jump = document.createElement("button");
  jump.className = "time-btn small";
  jump.textContent = `▶ ${fmtTime(scene.start_time)}`;
  jump.addEventListener("click", () =>
    jumpTo(scene.video_id, scene.seek_time, scene.youtube_fallback_url)
  );
  head.appendChild(jump);
  box.appendChild(head);

  const text = document.createElement("div");
  text.className = "scene-text";
  const rows = [
    ["말", scene.asr_text],
    ["자막", scene.ocr_text],
    ["장면", scene.caption],
  ].filter(([, value]) => value);
  for (const [key, value] of rows) {
    const line = document.createElement("div");
    const strong = document.createElement("b");
    strong.textContent = `${key}: `;
    line.appendChild(strong);
    line.appendChild(document.createTextNode(value));
    text.appendChild(line);
  }
  box.appendChild(text);
  return box;
}

/** 검색 결과 한 줄 = 영상 하나. 누르면 그 영상 안 장면이 펼쳐집니다. */
function videoRow(video, query) {
  const box = document.createElement("div");
  box.className = "video-row";

  const head = document.createElement("button");
  head.className = "video-head";
  head.setAttribute("aria-expanded", "false");

  const thumb = document.createElement("img");
  thumb.className = "video-thumb";
  thumb.src = video.thumbnail;
  thumb.alt = "";
  head.appendChild(thumb);

  const text = document.createElement("div");
  text.className = "video-text";
  const title = document.createElement("div");
  title.className = "card-title";
  title.textContent = video.title || video.video_id;
  text.appendChild(title);
  const sub = document.createElement("div");
  sub.className = "video-sub";
  // ⚠ 여기에 `candidate_count` 를 "장면 N개" 로 쓰면 안 됩니다. 그 값은 점수를 매기기
  //   전 후보 수라 실제보다 부풀려집니다. 진짜 개수는 펼친 뒤에야 알 수 있습니다.
  sub.textContent = `가장 잘 맞는 지점 ${fmtTime(video.best_time)}`;
  text.appendChild(sub);
  head.appendChild(text);

  const chev = document.createElement("span");
  chev.className = "chev";
  chev.textContent = "▾";
  head.appendChild(chev);
  box.appendChild(head);

  const body = document.createElement("div");
  body.className = "video-body hidden";
  box.appendChild(body);

  let loaded = false;
  head.addEventListener("click", async () => {
    const open = !body.classList.toggle("hidden");
    head.setAttribute("aria-expanded", String(open));
    chev.classList.toggle("open", open);
    if (!open || loaded) return; // 접을 때와 이미 불러온 뒤에는 다시 요청하지 않습니다
    loaded = true;
    body.innerHTML = "<p class='empty'>장면 찾는 중...</p>";
    try {
      const data = await api("/api/search", {
        method: "POST",
        body: JSON.stringify({
          query,
          video_id: video.video_id,
          top_k: SCENES_PER_VIDEO,
          with_answer: false, // Gemini 호출을 막습니다
          video_limit: 1,
        }),
      });
      body.innerHTML = "";
      if (!data.scenes.length) {
        body.innerHTML = "<p class='empty'>이 영상에서는 맞는 장면을 찾지 못했습니다.</p>";
        return;
      }
      const count = document.createElement("p");
      count.className = "video-sub";
      count.textContent = `장면 ${data.scenes.length}개`;
      body.appendChild(count);
      data.scenes.forEach((scene) => body.appendChild(sceneCard(scene, false)));
    } catch (err) {
      loaded = false; // 실패했으면 다시 펼칠 때 한 번 더 시도합니다
      body.innerHTML = "";
      const msg = document.createElement("p");
      msg.className = "empty";
      msg.textContent = `장면을 불러오지 못했습니다: ${err.message}`;
      body.appendChild(msg);
    }
  });

  return box;
}

async function runSearch() {
  const query = $("search-input").value.trim();
  if (!query) return;
  const results = $("search-results");
  const answer = $("answer");
  results.innerHTML = "";
  answer.textContent = "검색 중...";
  answer.classList.remove("hidden");

  try {
    const body = { query, top_k: 5, video_limit: VIDEO_LIMIT };
    if ($("scope-playlist").checked && selectedPlaylist) body.playlist_id = selectedPlaylist;
    const data = await api("/api/search", { method: "POST", body: JSON.stringify(body) });

    answer.textContent = data.answer || "";
    answer.classList.toggle("hidden", !data.answer);

    // 근거가 약하면 알려줍니다 (멘토 자문 3번).
    // 결과를 감추지는 않습니다 — 서버가 쓰는 기준(정규화 전 유사도 0.52)은 답이 있는
    // 질문도 48개 중 6개를 잘못 걸러냅니다. 감추면 그 6개를 영영 못 보게 되지만,
    // 경고로 두면 피해가 "불필요한 한 줄" 로 끝납니다.
    if (data.low_confidence) {
      const warn = document.createElement("p");
      warn.className = "warn";
      warn.textContent =
        "저장된 장면 중에 확실히 맞는 것을 찾지 못했습니다. 아래는 그나마 가까운 결과입니다.";
      results.appendChild(warn);
    }

    // "이런 영상 찾아줘" 는 영상을 고르는 질문이라 좁게 보여주고,
    // "간장 넣는 장면 찾아줘" 처럼 지점을 묻는 질문은 걸린 영상을 전부 늘어놓습니다.
    // 이 판별은 서버가 이미 해서 `intent` 로 내려줍니다(search.py · detect_intent).
    //
    // ⚠ 예전에는 영상 질문일 때 slice(0, 1) 로 **무조건 1개만** 남겼습니다. 그러면
    //   점수가 사실상 같은 영상이 있어도 버립니다. 실측: 김치찜 영상 2개를 저장하고
    //   "김치찜 영상 찾아줘" 를 묻자 서버는 0.9754 / 0.9473 둘 다 돌려줬는데
    //   화면에는 하나만 나왔습니다. 개수가 아니라 **1위와의 비율**로 잘라야
    //   비슷한 것은 같이 보이고 동떨어진 것만 접힙니다.
    const videos = data.videos;
    const cutoffRatio =
      data.intent === "video" ? VIDEO_INTENT_CUTOFF : RELATED_CUTOFF;
    if (!videos.length) {
      results.innerHTML = "<p class='empty'>맞는 영상을 찾지 못했습니다.</p>";
      return;
    }

    // 1위보다 한참 낮은 영상은 접어 둡니다(RELATED_CUTOFF 설명 참고).
    // videos 는 서버가 점수 내림차순으로 보내므로 잘리는 지점이 곧 경계입니다.
    const top = videos[0].score || 0;
    let cut = videos.length;
    if (top > 0) {
      cut = videos.findIndex((v) => v.score < top * cutoffRatio);
      if (cut < 0) cut = videos.length;
    }
    // 근거가 약하다고 말해놓고 영상을 열 개 늘어놓으면 말과 화면이 어긋납니다.
    // 엉뚱한 질문은 점수가 평평해서 위 비율 컷오프가 거의 안 걸리기 때문에,
    // 그럴 때는 따로 좁힙니다. 나머지는 "더 보기" 뒤에 그대로 남습니다.
    if (data.low_confidence) cut = Math.min(cut, 2);
    videos.slice(0, cut).forEach((video) => results.appendChild(videoRow(video, query)));

    const weak = videos.slice(cut);
    if (weak.length) {
      const more = document.createElement("button");
      more.className = "ghost small more-btn";
      more.textContent = `관련도 낮은 결과 ${weak.length}개 더 보기`;
      more.addEventListener("click", () => {
        more.remove();
        weak.forEach((video) => results.appendChild(videoRow(video, query)));
      });
      results.appendChild(more);
    }
  } catch (err) {
    answer.textContent = `검색 실패: ${err.message}`;
  }
}

// --- 재료 → 구매 링크 ------------------------------------------------------

function openTab(url) {
  chrome.tabs.create({ url });
}

// 기본: API 없이 검색 URL 조합. 고급: 서버에 네이버 키가 있으면 최저가를 붙입니다.
function ingredientRow(name) {
  const row = document.createElement("div");
  row.className = "ing";

  const label = document.createElement("span");
  label.className = "ing-name";
  label.textContent = name;
  row.appendChild(label);

  const price = document.createElement("a");
  price.className = "ing-price hidden";
  row.appendChild(price);

  const coupang = document.createElement("button");
  coupang.className = "shop-btn";
  coupang.textContent = "쿠팡";
  coupang.addEventListener("click", () =>
    openTab(`https://www.coupang.com/np/search?q=${encodeURIComponent(name)}`)
  );
  row.appendChild(coupang);

  const naver = document.createElement("button");
  naver.className = "shop-btn";
  naver.textContent = "N쇼핑";
  naver.addEventListener("click", () =>
    openTab(`https://search.shopping.naver.com/search/all?query=${encodeURIComponent(name)}`)
  );
  row.appendChild(naver);

  if (serverHealth?.shopping_price_enabled) {
    api(`/api/shopping?query=${encodeURIComponent(name)}`)
      .then((data) => {
        const item = data.items?.[0];
        if (!item || !item.price) return;
        price.textContent = `최저 ${item.price.toLocaleString("ko-KR")}원`;
        price.title = `${item.title} · ${item.mall}`;
        price.href = "#";
        price.addEventListener("click", (e) => {
          e.preventDefault();
          openTab(item.link);
        });
        price.classList.remove("hidden");
      })
      .catch(() => {}); // 가격은 덤입니다. 실패해도 링크는 그대로 씁니다.
  }
  return row;
}

// --- 사진 레시피 -----------------------------------------------------------

// 단계 시각(step.time)이 속한 장면 카드를 찾습니다. 걸치는 카드가 없으면 가장 가까운 것.
function segmentAt(segments, time) {
  let best = null;
  let bestDist = Infinity;
  for (const seg of segments || []) {
    if (time >= seg.start_time && time < seg.end_time) return seg;
    const dist = Math.abs(seg.start_time - time);
    if (dist < bestDist) {
      bestDist = dist;
      best = seg;
    }
  }
  return best;
}

function recipeStepCard(videoId, step, index, segment) {
  const card = document.createElement("div");
  card.className = "recipe-step";

  const no = document.createElement("span");
  no.className = "recipe-no";
  no.textContent = index + 1;
  card.appendChild(no);

  if (segment?.frame_url) {
    const img = document.createElement("img");
    img.className = "recipe-frame";
    img.src = `${API_BASE}${segment.frame_url}?u=${encodeURIComponent(userId)}`;
    img.alt = "";
    card.appendChild(img);
  }

  const main = document.createElement("div");
  main.className = "recipe-main";

  const label = document.createElement("div");
  label.className = "recipe-label";
  label.textContent = step.label;
  main.appendChild(label);

  const jump = document.createElement("button");
  jump.className = "time-btn small";
  jump.textContent = `▶ ${fmtTime(step.time)}`;
  jump.addEventListener("click", () =>
    jumpTo(
      videoId,
      Math.max(0, step.time - 1.5),
      `https://www.youtube.com/watch?v=${videoId}&t=${Math.max(0, Math.floor(step.time - 1.5))}s`
    )
  );
  main.appendChild(jump);

  card.appendChild(main);
  return card;
}

// --- 비교 탭 ---------------------------------------------------------------
// 같은 프레임·오디오를 다른 백엔드로 돌린 결과를 나란히 봅니다.
// 보관함/검색 탭이 쓰는 기본 조건 데이터는 건드리지 않습니다.

let compareVideoId = null;

async function loadCompareVideos() {
  const select = $("compare-video");
  const previous = select.value;
  try {
    const data = await api("/api/videos");
    const ready = data.videos.filter((v) => v.status === "ready");
    select.innerHTML = "";
    if (ready.length === 0) {
      const option = document.createElement("option");
      option.textContent = "분석 완료된 영상이 없습니다";
      option.value = "";
      select.appendChild(option);
      compareVideoId = null;
      $("condition-list").innerHTML = "";
      return;
    }
    for (const video of ready) {
      const option = document.createElement("option");
      option.value = video.video_id;
      option.textContent = video.title || video.video_id;
      select.appendChild(option);
    }
    select.value = ready.some((v) => v.video_id === previous) ? previous : ready[0].video_id;
    compareVideoId = select.value;
    await loadConditions();
  } catch (err) {
    select.innerHTML = "";
    toast(`영상 목록을 못 불러왔습니다: ${err.message}`, true);
  }
}

function conditionCard(run) {
  const box = document.createElement("div");
  box.className = `cond${run.ready ? " ready" : ""}`;

  const head = document.createElement("div");
  head.className = "cond-head";

  const name = document.createElement("div");
  name.className = "cond-name";
  name.textContent = run.label;
  head.appendChild(name);

  const done = run.status === "ready";
  const running = run.status && !["ready", "failed", "none"].includes(run.status);

  const button = document.createElement("button");
  button.className = done ? "small" : "primary small";
  if (!run.ready) {
    // 왜 못 누르는지 버튼에 그대로 씁니다.
    button.textContent = "패키지 설치 필요 ↓";
    button.title = run.install_hint;
  } else {
    button.textContent = running ? run.status_label : done ? "다시 분석" : "이 조건으로 분석";
  }
  button.disabled = !run.ready || running;
  button.addEventListener("click", () => runCondition(run.condition || run.name));
  head.appendChild(button);
  box.appendChild(head);

  const desc = document.createElement("div");
  desc.className = "cond-desc";
  desc.textContent = run.description;
  box.appendChild(desc);

  const backends = document.createElement("div");
  backends.className = "cond-backends";
  backends.textContent = `음성 ${run.backends.asr} · 자막 ${run.backends.ocr} · 글좌표 ${run.backends.text_embed} · 그림좌표 ${run.backends.image_embed}`;
  box.appendChild(backends);

  const status = document.createElement("div");
  status.className = `status ${run.status}`;
  status.textContent = done
    ? `✅ 분석됨 · 장면 ${run.counts?.segments ?? "?"}장 · ${run.total_seconds ?? "?"}초`
    : run.status === "failed"
      ? `실패: ${run.error}`
      : running
        ? run.status_label
        : "아직 안 돌렸습니다";
  box.appendChild(status);

  if (!run.ready) {
    const install = document.createElement("div");
    install.className = "cond-install";
    const label = document.createElement("div");
    label.textContent = "이 버튼을 누르려면 서버 쪽에서 먼저 설치하세요:";
    label.style.marginBottom = "4px";
    const code = document.createElement("code");
    code.textContent = run.install_hint;
    const note = document.createElement("div");
    note.style.marginTop = "4px";
    note.textContent = "모델 가중치 약 6GB (첫 실행 때 자동 다운로드) · 설치 후 서버를 다시 켜야 합니다";
    install.append(label, code, note);
    box.appendChild(install);
  }
  return box;
}

async function loadConditions() {
  const list = $("condition-list");
  if (!compareVideoId) {
    list.innerHTML = "";
    return;
  }
  try {
    const data = await api(`/api/videos/${compareVideoId}/conditions`);
    list.innerHTML = "";
    data.runs.forEach((run) => list.appendChild(conditionCard(run)));
    renderCompareTable(data.runs);
  } catch (err) {
    list.innerHTML = `<p class="empty">${err.message}</p>`;
  }
}

// 확정본 9장 "정보원별 기여도" / "처리 시간 분해" 에 그대로 쓰는 표입니다.
const COMPARE_ROWS = [
  ["장면 카드", (r) => r.counts?.segments, "more"],
  ["말이 담긴 카드", (r) => r.counts?.segments_with_asr, "more"],
  ["자막이 담긴 카드", (r) => r.counts?.segments_with_ocr, "more"],
  ["장면설명이 담긴 카드", (r) => r.counts?.segments_with_caption, "more"],
  ["받아적은 구간", (r) => r.counts?.utterances, "more"],
  ["음성 분석(초)", (r) => r.timings?.transcribe, "less"],
  ["화면 분석(초)", (r) => r.timings?.frames, "less"],
  ["좌표 변환(초)", (r) => r.timings?.embedding, "less"],
  ["총 처리 시간(초)", (r) => r.total_seconds, "less"],
];

function renderCompareTable(runs) {
  const done = runs.filter((r) => r.status === "ready");
  const wrap = $("compare-table-wrap");
  if (done.length < 1) {
    wrap.classList.add("hidden");
    return;
  }
  wrap.classList.remove("hidden");

  const table = $("compare-table");
  table.innerHTML = "";
  const thead = table.createTHead().insertRow();
  thead.insertCell().outerHTML = "<th>항목</th>";
  done.forEach((r) => (thead.insertCell().outerHTML = `<th>${r.label}</th>`));

  const body = table.createTBody();
  for (const [label, pick, better] of COMPARE_ROWS) {
    const values = done.map(pick);
    if (values.every((v) => v === undefined || v === null)) continue;

    const numeric = values.filter((v) => typeof v === "number");
    let best = null;
    if (numeric.length > 1 && new Set(numeric).size > 1) {
      best = better === "more" ? Math.max(...numeric) : Math.min(...numeric);
    }

    const row = body.insertRow();
    row.insertCell().textContent = label;
    values.forEach((value) => {
      const cell = row.insertCell();
      cell.textContent = value ?? "-";
      if (best !== null && value === best) cell.className = "win";
    });
  }
}

async function runCondition(condition) {
  if (!compareVideoId) return;
  try {
    await api(`/api/videos/${compareVideoId}/conditions/${condition}`, { method: "POST" });
    toast("재분석을 시작했습니다. 로컬 모델은 몇 분 걸립니다.");
    await loadConditions();
    startPolling();
  } catch (err) {
    toast(`재분석 실패: ${err.message}`, true);
  }
}

function compareBlock(result) {
  const box = document.createElement("div");
  box.className = "cond-block";

  const title = document.createElement("h5");
  title.textContent = result.label || result.condition;
  box.appendChild(title);

  if (!result.available) {
    const empty = document.createElement("div");
    empty.className = "card-sub";
    empty.textContent = "아직 이 조건으로 분석하지 않았습니다.";
    box.appendChild(empty);
    return box;
  }

  const meta = document.createElement("div");
  meta.className = "cond-backends";
  const w = result.weights;
  meta.textContent = `${result.query_type} · 글${w.dense.toFixed(2)}·단어${w.lexical.toFixed(2)}·그림${w.image.toFixed(2)}`;
  box.appendChild(meta);

  if (!result.scenes.length) {
    const empty = document.createElement("div");
    empty.className = "card-sub";
    empty.textContent = "결과 없음";
    box.appendChild(empty);
    return box;
  }
  result.scenes.forEach((scene) => box.appendChild(sceneCard(scene)));
  return box;
}

async function runCompare() {
  const query = $("compare-query").value.trim();
  if (!query || !compareVideoId) return;
  const results = $("compare-results");
  const agreement = $("compare-agreement");
  results.innerHTML = "<p class='empty'>비교 중...</p>";
  agreement.classList.add("hidden");

  try {
    const data = await api("/api/compare", {
      method: "POST",
      body: JSON.stringify({ video_id: compareVideoId, query, top_k: 3 }),
    });
    results.innerHTML = "";
    data.results.forEach((result) => results.appendChild(compareBlock(result)));

    if (data.agreement) {
      agreement.className = `agreement ${data.agreement.same ? "same" : "diff"}`;
      agreement.textContent = data.agreement.same
        ? `✅ 두 조건이 같은 지점을 가리킵니다 (차이 ${data.agreement.gap_seconds}초)`
        : `⚠ 두 조건이 다른 지점을 가리킵니다 (차이 ${data.agreement.gap_seconds}초)`;
      agreement.classList.remove("hidden");
    }
  } catch (err) {
    results.innerHTML = `<p class="empty">비교 실패: ${err.message}</p>`;
  }
}

// --- 상세 화면 -------------------------------------------------------------

async function openDetail(videoId) {
  const panel = $("detail");
  const body = $("detail-body");
  body.innerHTML = "불러오는 중...";
  panel.classList.remove("hidden");

  try {
    const video = await api(`/api/videos/${videoId}`);
    body.innerHTML = "";

    const title = document.createElement("h3");
    title.textContent = video.title || videoId;
    body.appendChild(title);

    const sub = document.createElement("div");
    sub.className = "card-sub";
    sub.textContent = `${video.channel || ""} · ${video.status_label} · 장면 ${
      video.segments.length
    }개`;
    body.appendChild(sub);

    if (video.summary) {
      const headline = document.createElement("p");
      headline.textContent = video.summary.headline || "";
      body.appendChild(headline);

      if (video.summary.keywords?.length) {
        const wrap = document.createElement("div");
        video.summary.keywords.forEach((kw) => {
          const chip = document.createElement("span");
          chip.className = "kw";
          chip.textContent = kw;
          wrap.appendChild(chip);
        });
        body.appendChild(wrap);
      }

      if (video.summary.ingredients?.length) {
        const heading = document.createElement("h3");
        heading.textContent = "재료 · 구매 링크";
        body.appendChild(heading);
        const list = document.createElement("div");
        list.className = "ings";
        video.summary.ingredients.forEach((name) => list.appendChild(ingredientRow(name)));
        body.appendChild(list);
      }

      if (video.summary.steps?.length) {
        const heading = document.createElement("h3");
        heading.textContent = "사진 레시피";
        body.appendChild(heading);
        const list = document.createElement("div");
        list.className = "recipe";
        video.summary.steps.forEach((step, index) => {
          const segment = segmentAt(video.segments, step.time);
          list.appendChild(recipeStepCard(videoId, step, index, segment));
        });
        body.appendChild(list);
      }
    }

    const heading = document.createElement("h3");
    heading.textContent = "이 영상 안에서 찾기";
    body.appendChild(heading);

    const box = document.createElement("div");
    box.className = "search-box";
    const input = document.createElement("input");
    input.placeholder = "예: 감자 언제 넣어?";
    const go = document.createElement("button");
    go.className = "primary small";
    go.textContent = "검색";
    box.append(input, go);
    body.appendChild(box);

    const answer = document.createElement("div");
    answer.className = "answer hidden";
    body.appendChild(answer);
    const results = document.createElement("div");
    results.className = "list";
    body.appendChild(results);

    const search = async () => {
      const query = input.value.trim();
      if (!query) return;
      answer.textContent = "검색 중...";
      answer.classList.remove("hidden");
      results.innerHTML = "";
      try {
        const data = await api("/api/search", {
          method: "POST",
          body: JSON.stringify({ query, video_id: videoId, top_k: 5 }),
        });
        answer.textContent = data.answer || "";
        data.scenes.forEach((scene) => results.appendChild(sceneCard(scene)));
      } catch (err) {
        answer.textContent = `검색 실패: ${err.message}`;
      }
    };
    go.addEventListener("click", search);
    input.addEventListener("keydown", (e) => e.key === "Enter" && search());

    $("detail-delete").onclick = async () => {
      if (!confirm("이 영상을 보관함에서 지울까요?")) return;
      await api(`/api/videos/${videoId}`, { method: "DELETE" });
      panel.classList.add("hidden");
      await loadLibrary();
      toast("삭제했습니다.");
    };
  } catch (err) {
    body.textContent = `불러오지 못했습니다: ${err.message}`;
  }
}

// --- 시작 -----------------------------------------------------------------

async function checkServer() {
  const pill = $("server-state");
  try {
    const health = await api("/api/health");
    serverHealth = health;
    const b = health.backends;
    pill.textContent = health.ok
      ? `${health.device} · 음성 ${b.asr} · 자막 ${b.ocr}${health.image_search_enabled ? " · 그림검색" : ""}`
      : health.reason;
    pill.title = JSON.stringify(health.backends, null, 2);
    pill.className = `pill ${health.ok ? "pill-on" : "pill-off"}`;
  } catch (_) {
    pill.textContent = "서버 꺼짐";
    pill.className = "pill pill-off";
  }
}

function bindEvents() {
  $("save-btn").addEventListener("click", saveCurrent);
  $("new-playlist").addEventListener("click", createPlaylist);
  $("playlist-select").addEventListener("change", async (e) => {
    selectedPlaylist = e.target.value;
    await loadLibrary();
  });
  $("search-btn").addEventListener("click", runSearch);
  $("search-input").addEventListener("keydown", (e) => e.key === "Enter" && runSearch());
  $("detail-back").addEventListener("click", () => $("detail").classList.add("hidden"));

  $("compare-video").addEventListener("change", async (e) => {
    compareVideoId = e.target.value;
    $("compare-results").innerHTML = "";
    $("compare-agreement").classList.add("hidden");
    await loadConditions();
  });
  $("compare-run").addEventListener("click", runCompare);
  $("compare-query").addEventListener("keydown", (e) => e.key === "Enter" && runCompare());

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", async () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      for (const name of ["library", "search", "compare"]) {
        $(`tab-${name}`).classList.toggle("hidden", tab.dataset.tab !== name);
      }
      if (tab.dataset.tab === "compare") await loadCompareVideos();
    });
  });

  chrome.runtime.onMessage.addListener((message, sender) => {
    // 쇼츠를 스크롤로 넘겼을 때 — 지금 실제로 보고 있는 탭에서 온 알림만 반영합니다.
    // (여러 유튜브 탭이 열려 있으면 백그라운드 탭도 이 메시지를 보낼 수 있습니다.)
    if (message?.type === "SHORTS_CHANGED") {
      activeYoutubeTab().then((tab) => {
        if (tab && sender.tab && tab.id === sender.tab.id) setCurrentInfo(message.info);
      });
    }
    // 추출 진행률
    if (message?.type === "CAPTURE_PROGRESS") {
      const label =
        message.phase === "uploading"
          ? "서버로 전송 중입니다..."
          : "저장 창에서 영상을 재생하며 분석 중입니다. 다른 쇼츠를 보셔도 됩니다.";
      showCapture(message.phase !== "uploaded", message.ratio || 0, label);
    }
    return false;
  });
  chrome.tabs.onActivated.addListener(refreshCurrentVideo);
  // 쇼츠는 스크롤만 해도 **같은 탭 안에서** 영상이 바뀝니다. onActivated(탭 전환)만
  // 듣고 있으면 Side Panel 이 이전 영상 정보를 그대로 들고 있어, 저장 버튼이 엉뚱한
  // video_id 를 보냅니다(이미 저장된 영상이면 "이미 저장된 영상입니다" 로 막힙니다).
  // 유튜브는 History API 로 주소를 바꾸므로 onUpdated 의 changeInfo.url 로 잡힙니다.
  chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    if (changeInfo.url && tab.active) refreshCurrentVideo();
  });
}

(async function init() {
  bindEvents();
  await getUserId();
  await checkServer();
  await loadPlaylists();
  await loadLibrary();
  await refreshCurrentVideo();
  startPolling();
})();
