# 쇼츠 AI 보관함 — Claude Code 작업 지침

유튜브 쇼츠를 저장하면 4초 장면 카드로 분석해 두고, 자연어로 검색해 그 초로 이동시키는
크롬 확장 + FastAPI 서버. 전체 설명은 `README.md`, 진행 상황 총정리는 `docs/PROGRESS.md`.

## 먼저 읽을 것
- `docs/PROGRESS.md` — **★ 통합본.** 9/18 자문 이후 무엇을 고쳤고 지금 성적이 얼마인지.
  **무엇을 먼저 할지도 여기 7장.** 상황 파악은 이 문서 하나로 끝납니다
- `README.md` — 실행법, 코드 구조, 담당 경계
- `docs/eval-guide.md` — 질문셋·평가셋 만드는 법. 평가 작업을 도울 때 기준이 되는 문서
- `server/config.py` — 모든 설정과 조건(condition) 정의가 여기 한 곳
- 세부 기록은 `docs/fixes-20261005.md`(현준) · `docs/fixes-20261010.md`(동현) ·
  `SigLIP2_실험기록_20260914.md` · `docs/mentor-feedback.md`(자문 원문)
- ⚠ `docs/MENTOR_BRIEF.md` · `docs/search-logic.md`(9/22) 와 `docs/team-meeting.md`(9/29) 는
  **낡았습니다.** 설계 의도와 코드 위치는 유효하지만 설정값·측정치·"미해결" 목록은 믿지 마세요

## 환경
- Python 3.14 (WhisperX 미지원 → faster-whisper 폴백이 코드에 있음). 가상환경 `.venv`
- Windows 한글 콘솔: 서버 띄우기 전 `$env:PYTHONUTF8 = "1"` (cp949 인코딩 오류 예방)
- 서버: `python -m uvicorn server.main:app --port 8000` — **`--reload` 금지** (로컬 Qdrant는 프로세스 하나만)
- `.env` 는 저장소에 없음. `.env.example` 복사 후 `GEMINI_API_KEY` 채우기
- **Gemini 무료 티어는 모델당 하루 20회.** 영상 1개가 쓰는 호출 = 음성 + 격자 캡션.
  `ASR_BACKEND=faster-whisper` + `MAX_GEMINI_CALLS_PER_VIDEO=1` + `GRID_FRAMES=12` 로
  영상당 1회(=하루 20개). 셋 다 `gemini` 로 두면 할당량이 떨어질 때 음성·자막·장면이
  동시에 비어 저장이 통째로 실패합니다
- `data/` (SQLite, Qdrant, 프레임·오디오) 는 저장소에 없음 — 새 컴퓨터에선 확장으로 쇼츠를 새로 저장해야 데이터가 생김
- 로컬 모델 가중치 약 6.3GB, 첫 실행 때 HF/GitHub에서 자동 다운로드. 로컬 모델 두 개 이상 동시 실행 금지 (16GB RAM에서 OOM)

## 설계 원칙 (바꾸지 말 것)
1. 서버는 유튜브에 접속하지 않는다. 프레임·오디오는 확장이 브라우저에서 뽑아 올린다
2. 모델 단계마다 `gemini` 대체품과 로컬 백엔드를 **같은 인터페이스**로 둔다. 전환은 `.env` 한 줄 또는 `use_condition()`
3. `DEVICE` 는 `config.py` 한 곳. 코드에 `"cuda"` 직접 쓰지 않는다
4. Gemini 호출은 영상당 `MAX_GEMINI_CALLS_PER_VIDEO` 를 코드에서 강제
   — **현재 값은 1** (무료 티어가 모델당 하루 20회라 3에서 낮춤, 2026-09-22).
   상한을 코드로 강제한다는 원칙은 그대로이고 숫자만 바뀐 것입니다
5. 모든 조회·검색·삭제는 `user_id` 필터 필수 (`tools/test_user_isolation.py` 가 검증)
6. 제품과 실험은 `server/pipeline/analyze.py` 같은 코드 경로를 쓴다. 실험용 별도 경로 만들지 않는다
7. 장면 카드 JSON 모양 `{segment_id, start_time, end_time, asr_text, ocr_text, caption, frame_path}` 유지
8. 프레임 파일 이름이 시각: `00022000.jpg` = 22.0초

## 검증
```
python -m tools.smoke_test            # Gemini 없이
python -m tools.test_user_isolation   # 사용자 격리 ★
python -m tools.check_models          # 그날 되는 Gemini 모델
python -m tools.e2e_test              # 서버 띄운 뒤, 전 구간
python -m tools.run_condition --list  # 조건·영상 목록
python -m tools.ablation              # 제거법 계획 (서버 끄고, --yes 로 실행)
python -m tools.cost_report           # 영상당 처리 시간·호출 수 (자문 4)
python -m tools.merge_experiment      # 의미 단위 병합 전후 (자문 1, 서버 끄고)
python -m tools.threshold_sweep       # 거절 임계값 곡선 (자문 3, 서버 켜고)
```

## 코드 스타일
- 주석·문서·커밋 메시지는 한국어. 파일 첫 docstring에 "단계 N · 무엇. 담당 X" 형식
- 커밋 제목은 `feat:` / `fix:` 접두사 + 한국어 한 줄, 본문에 왜 바꿨는지
- 파일 끝 줄바꿈은 LF

## 지금 상태 / 다음 할 일
- **멘토 자문 보완 5가지가 전부 처리됐습니다** (`docs/PROGRESS.md` 5-2~5-6장).
  전부 "좋아졌다" 는 아닙니다 — 자문 1(병합)은 **해보고 채택하지 않았고 그게 결과**입니다
- 색인은 전부 로컬 모델(faster-whisper / EasyOCR / BGE-M3 1024차원 / SigLIP2),
  **장면 설명만 Gemini.** 저장된 영상 18개 · 장면 카드 203개 (`eval/video_list.md`)
- 채움률: 말 80% · 자막 94% · **설명 55%** (할당량으로 5개 영상에 캡션 없음).
  **지금 모든 수치가 이 상태에서 잰 것이라 확정값이 아닙니다**
- 평가셋 질문 64개 / 라벨 113줄. 현행 nDCG@5 **0.756** (기준선 음성만 0.728, +0.028)
  측정: `python -m tools.eval_search --all` · 최신 결과 `eval/result_20261013.json`
- 자문 3 적용 중: `NO_ANSWER_THRESHOLD=0.52` 로 근거가 약하면 경고를 띄웁니다
- 자문 1 결과: `MERGE_SIMILARITY=0` **(꺼짐)**. 병합은 검색을 좋게 하지만 top-5 가 짚는
  정답 수가 줄어 손해였고, 오라클(천장)까지 재서 확인했습니다. 코드는 남겨 뒀습니다
- **다음 할 일은 `docs/PROGRESS.md` 7장.** 1순위는 캡션 5개 복구 후 재측정
- ⚠ `tools.make_labeling_sheet --force` 는 지금 시트를 덮어씁니다 (라벨 113줄이 들어 있음)
- ⚠ 한글 질의로 API 를 시험할 때 PowerShell `Invoke-RestMethod` 는 UTF-8 로 안 보냅니다.
  Python `urllib` 로 하세요 (PowerShell 로 재면 유사도가 엉뚱하게 낮게 나옵니다)
