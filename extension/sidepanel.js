const API_BASE = "http://localhost:8000";
const POLL_MS = 3000;

let userId = null;
let currentInfo = null;
let playlists = [];
let selectedPlaylist = "";
let pollTimer = null;

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
// 그동안 이 탭에 머물러야 하므로 안내를 크게 띄웁니다.
function showCapture(visible, ratio = 0, message = "") {
  const box = $("capture");
  box.classList.toggle("hidden", !visible);
  $("capture-fill").style.width = `${Math.round(ratio * 100)}%`;
  if (message) $("capture-msg").textContent = message;
}

async function saveCurrent() {
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
    const tab = await activeYoutubeTab();
    if (!tab) throw new Error("유튜브 탭을 찾지 못했습니다.");

    showCapture(true, 0, "영상을 재생하며 분석 준비 중입니다. 이 탭에 머물러 주세요.");
    const result = await askContentScript(tab.id, {
      type: "CAPTURE_START",
      videoId,
      settings: accepted.capture,
      apiBase: API_BASE,
      userId: await getUserId(),
    });
    showCapture(false);

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
      if (data.pending.length === 0) {
        clearInterval(pollTimer);
        pollTimer = null;
        await loadPlaylists();
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

function sceneCard(scene) {
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

  const label = document.createElement("div");
  label.className = "card-title";
  label.textContent = scene.title || scene.video_id;
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

async function runSearch() {
  const query = $("search-input").value.trim();
  if (!query) return;
  const results = $("search-results");
  const answer = $("answer");
  results.innerHTML = "";
  answer.textContent = "검색 중...";
  answer.classList.remove("hidden");

  try {
    const body = { query, top_k: 5 };
    if ($("scope-playlist").checked && selectedPlaylist) body.playlist_id = selectedPlaylist;
    const data = await api("/api/search", { method: "POST", body: JSON.stringify(body) });

    answer.textContent = data.answer || "";
    answer.classList.toggle("hidden", !data.answer);
    if (data.scenes.length === 0) {
      results.innerHTML = "<p class='empty'>맞는 장면을 찾지 못했습니다.</p>";
      return;
    }
    data.scenes.forEach((scene) => results.appendChild(sceneCard(scene)));
  } catch (err) {
    answer.textContent = `검색 실패: ${err.message}`;
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

      if (video.summary.steps?.length) {
        const heading = document.createElement("h3");
        heading.textContent = "주요 단계";
        body.appendChild(heading);
        const list = document.createElement("ul");
        list.className = "steps";
        video.summary.steps.forEach((step) => {
          const li = document.createElement("li");
          const button = document.createElement("button");
          button.className = "time-btn small";
          button.textContent = fmtTime(step.time);
          button.addEventListener("click", () =>
            jumpTo(
              videoId,
              Math.max(0, step.time - 1.5),
              `https://www.youtube.com/watch?v=${videoId}&t=${Math.max(0, Math.floor(step.time - 1.5))}s`
            )
          );
          li.appendChild(button);
          li.appendChild(document.createTextNode(step.label));
          list.appendChild(li);
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

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      $("tab-library").classList.toggle("hidden", tab.dataset.tab !== "library");
      $("tab-search").classList.toggle("hidden", tab.dataset.tab !== "search");
    });
  });

  chrome.runtime.onMessage.addListener((message) => {
    // 쇼츠를 스크롤로 넘겼을 때
    if (message?.type === "SHORTS_CHANGED") setCurrentInfo(message.info);
    // 추출 진행률
    if (message?.type === "CAPTURE_PROGRESS") {
      const label =
        message.phase === "uploading"
          ? "서버로 전송 중입니다..."
          : "영상을 재생하며 분석 준비 중입니다. 이 탭에 머물러 주세요.";
      showCapture(message.phase !== "uploaded", message.ratio || 0, label);
    }
    return false;
  });
  chrome.tabs.onActivated.addListener(refreshCurrentVideo);
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
