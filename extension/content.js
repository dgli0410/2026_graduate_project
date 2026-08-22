// 유튜브 페이지 안에서 도는 부분. 담당 A.
//
// 하는 일
//  (1) 지금 보고 있는 쇼츠 정보 읽기            — 단계 1
//  (2) 재생하면서 화면·소리 뽑아 서버로 올리기  — 단계 2  ★ 핵심
//  (3) 특정 시점으로 이동                       — 단계 13
//  (4) 쇼츠 전환 감지
//
// 서버는 유튜브에 한 번도 접속하지 않습니다. 영상은 이미 이 브라우저 안에 있습니다.

// --- 영상 찾기 -------------------------------------------------------------

function currentVideoId() {
  const url = new URL(location.href);
  const shorts = url.pathname.match(/^\/shorts\/([\w-]{5,})/);
  if (shorts) return shorts[1];
  return url.searchParams.get("v");
}

function activeReel() {
  // 쇼츠는 여러 개가 DOM 에 동시에 존재합니다. 지금 보이는 것만 골라야 합니다.
  return (
    document.querySelector("ytd-reel-video-renderer[is-active]") ||
    document.querySelector("#shorts-player") ||
    document
  );
}

function activeVideoElement() {
  const scope = activeReel();
  const scoped = scope.querySelector && scope.querySelector("video");
  if (scoped && scoped.readyState >= 2) return scoped;

  const videos = Array.from(document.querySelectorAll("video"));
  const visible = videos.filter((v) => {
    const r = v.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.top < window.innerHeight && r.bottom > 0;
  });
  // 스크롤 시 요소가 교체되므로 readyState 를 확인하고 접근합니다.
  return (
    visible.find((v) => v.readyState >= 2 && !v.paused) ||
    visible.find((v) => v.readyState >= 2) ||
    visible[0] ||
    videos[0] ||
    null
  );
}

function textOf(root, selectors) {
  for (const sel of selectors) {
    const el = root.querySelector ? root.querySelector(sel) : null;
    const text = el && (el.textContent || "").trim();
    if (text) return text;
  }
  return "";
}

// document.title 은 "(357) 제목 - YouTube" 처럼 알림 개수가 앞에 붙습니다.
function cleanTitle(raw) {
  return String(raw || "")
    .replace(/^\(\d+\)\s*/, "")
    .replace(/\s*-\s*YouTube\s*$/, "")
    .trim();
}

function readVideoInfo() {
  const videoId = currentVideoId();
  if (!videoId) return null;

  const reel = activeReel();
  const title =
    textOf(reel, [
      "yt-shorts-video-title-view-model h2",
      "yt-shorts-video-title-view-model",
      ".ytShortsVideoTitleViewModelShortsVideoTitle",
      "h2.title",
      "#title h2",
      ".ytd-reel-player-header-renderer #video-title",
    ]) ||
    textOf(document, ["h1.ytd-watch-metadata", "#title h1", "meta[name='title']"]) ||
    cleanTitle(document.title);

  const channel =
    textOf(reel, ["#channel-name a", "yt-reel-channel-bar-view-model a", "#text-container"]) ||
    textOf(document, ["#owner #channel-name a", "ytd-channel-name a"]);

  const video = activeVideoElement();
  const duration = video && isFinite(video.duration) ? video.duration : 0;

  return {
    videoId,
    title: cleanTitle(title).slice(0, 200),
    channel: channel.slice(0, 100),
    duration: Math.round(duration * 100) / 100,
    isShorts: location.pathname.startsWith("/shorts/"),
    url: location.href,
  };
}

// --- 단계 2 · 화면과 소리 추출 ★ ------------------------------------------

let capturing = false;

function report(videoId, phase, ratio, message) {
  chrome.runtime
    .sendMessage({ type: "CAPTURE_PROGRESS", videoId, phase, ratio, message })
    .catch(() => {});
}

function frameDiff(a, b) {
  if (!a || !b || a.length !== b.length) return 1;
  let sum = 0;
  for (let i = 0; i < a.length; i += 4) sum += Math.abs(a[i] - b[i]);
  return sum / (a.length / 4) / 255;
}

async function captureAndUpload(videoId, settings, apiBase, userId) {
  if (capturing) return { ok: false, reason: "already_capturing" };
  const video = activeVideoElement();
  if (!video) return { ok: false, reason: "no_video_element" };
  if (currentVideoId() !== videoId) return { ok: false, reason: "different_video" };

  capturing = true;
  try {
    const width = settings.frame_width || 540;
    const height = settings.frame_height || 960;
    const interval = (settings.frame_interval || 0.5) * 1000;
    const quality = settings.frame_quality || 0.75;
    const threshold = settings.dedup_threshold ?? 0.02;

    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });

    // 중복 판정용 축소 캔버스
    const tiny = document.createElement("canvas");
    tiny.width = 32;
    tiny.height = 56;
    const tinyCtx = tiny.getContext("2d", { willReadFrequently: true });

    // --- 소리 (검증된 방식) ---
    let recorder = null;
    const audioChunks = [];
    try {
      const stream = video.captureStream ? video.captureStream() : null;
      const tracks = stream ? stream.getAudioTracks() : [];
      if (tracks.length) {
        recorder = new MediaRecorder(new MediaStream(tracks));
        recorder.ondataavailable = (e) => e.data && e.data.size && audioChunks.push(e.data);
        recorder.start();
      }
    } catch (err) {
      console.warn("[보관함] 오디오 캡처 실패, 화면만 사용합니다:", err);
    }

    // --- 화면 ---
    const duration = isFinite(video.duration) && video.duration > 0 ? video.duration : 60;
    video.currentTime = 0;
    try {
      await video.play();
    } catch (_) {}

    const frames = [];
    let previous = null;
    let lastTime = -1;

    await new Promise((resolve) => {
      const timer = setInterval(async () => {
        const now = video.currentTime;
        // 쇼츠는 끝나면 처음으로 되감깁니다. 되감기면 한 바퀴 끝난 것입니다.
        if (now + 0.05 < lastTime || now >= duration - 0.05) {
          clearInterval(timer);
          resolve();
          return;
        }
        lastTime = now;

        try {
          ctx.drawImage(video, 0, 0, width, height);
          tinyCtx.drawImage(canvas, 0, 0, tiny.width, tiny.height);
          const pixels = tinyCtx.getImageData(0, 0, tiny.width, tiny.height).data;

          // 앞 프레임과 거의 같으면 건너뜁니다 (용량 20~30% 절감)
          if (previous === null || frameDiff(previous, pixels) > threshold) {
            previous = pixels;
            const blob = await new Promise((r) => canvas.toBlob(r, "image/jpeg", quality));
            if (blob) frames.push({ ms: Math.round(now * 1000), blob });
          }
        } catch (err) {
          console.warn("[보관함] 프레임 추출 실패:", err);
        }
        report(videoId, "capturing", Math.min(0.95, now / duration), "");
      }, interval);
    });

    let audioBlob = null;
    if (recorder && recorder.state !== "inactive") {
      audioBlob = await new Promise((resolve) => {
        recorder.onstop = () => resolve(new Blob(audioChunks, { type: "audio/webm" }));
        recorder.stop();
      });
    }

    if (!frames.length) return { ok: false, reason: "no_frames" };

    // --- 업로드 ---
    report(videoId, "uploading", 0.97, "");
    const form = new FormData();
    for (const frame of frames) {
      form.append("frames", frame.blob, `${String(frame.ms).padStart(8, "0")}.jpg`);
    }
    if (audioBlob && audioBlob.size) {
      const wav = await toWav(audioBlob);
      if (wav) form.append("audio", wav, "audio.wav");
    }
    form.append("duration", String(duration));

    const response = await fetch(`${apiBase}/api/videos/${videoId}/media`, {
      method: "POST",
      headers: { "X-User-Id": userId },
      body: form,
    });
    if (!response.ok) {
      let detail = String(response.status);
      try {
        detail = (await response.json()).detail || detail;
      } catch (_) {}
      return { ok: false, reason: detail };
    }
    const result = await response.json();
    report(videoId, "uploaded", 1, "");
    return { ok: true, frames: frames.length, result };
  } finally {
    capturing = false;
  }
}

// MediaRecorder 의 webm(opus) 을 16kHz 모노 wav 로 바꿉니다.
// WhisperX 와 Gemini 둘 다 wav 를 바로 받으므로 서버에 ffmpeg 가 필요 없습니다.
async function toWav(blob) {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
    const rate = 16000;
    const offline = new OfflineAudioContext(1, Math.ceil(decoded.duration * rate), rate);
    const source = offline.createBufferSource();
    source.buffer = decoded;
    source.connect(offline.destination);
    source.start();
    const rendered = await offline.startRendering();
    ctx.close();
    return encodeWav(rendered.getChannelData(0), rate);
  } catch (err) {
    console.warn("[보관함] wav 변환 실패:", err);
    return null;
  }
}

function encodeWav(samples, rate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const writeText = (offset, text) => {
    for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
  };
  writeText(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  writeText(8, "WAVEfmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeText(36, "data");
  view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buffer], { type: "audio/wav" });
}

// --- 단계 13 · 시점 이동 ---------------------------------------------------

function seekTo(videoId, seconds) {
  if (currentVideoId() !== videoId) return { ok: false, reason: "different_video" };
  const video = activeVideoElement();
  if (!video || video.readyState < 2) return { ok: false, reason: "not_ready" };
  try {
    video.currentTime = Math.max(0, seconds);
    const playing = video.play();
    if (playing && playing.catch) playing.catch(() => {});
    return { ok: true, at: video.currentTime };
  } catch (err) {
    return { ok: false, reason: String(err) };
  }
}

// --- 메시지 처리 -----------------------------------------------------------

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "GET_VIDEO_INFO") {
    sendResponse(readVideoInfo());
    return false;
  }
  if (message?.type === "SEEK_TO") {
    sendResponse(seekTo(message.videoId, message.seconds));
    return false;
  }
  if (message?.type === "CAPTURE_START") {
    captureAndUpload(message.videoId, message.settings, message.apiBase, message.userId).then(
      sendResponse
    );
    return true; // 비동기 응답
  }
  return false;
});

// --- 쇼츠 전환 감지 --------------------------------------------------------
// 스크롤로 다음 쇼츠로 넘어가도 페이지가 새로 열리지 않으므로 직접 감지합니다.
let lastVideoId = currentVideoId();

function notifyIfChanged() {
  if (capturing) return; // 추출 중에는 흔들지 않습니다
  const id = currentVideoId();
  if (id && id !== lastVideoId) {
    lastVideoId = id;
    const info = readVideoInfo();
    if (info) chrome.runtime.sendMessage({ type: "SHORTS_CHANGED", info }).catch(() => {});
  }
}

document.addEventListener("yt-navigate-finish", notifyIfChanged);
window.addEventListener("popstate", notifyIfChanged);
setInterval(notifyIfChanged, 800); // 위 이벤트가 안 뜨는 경우 대비
