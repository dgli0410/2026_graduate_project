# 쇼츠 AI 보관함 — Claude Code 작업 지침

유튜브 쇼츠를 저장하면 4초 장면 카드로 분석해 두고, 자연어로 검색해 그 초로 이동시키는
크롬 확장 + FastAPI 서버. 전체 설명은 `README.md`, 계획·실험·실측치 총정리는 `docs/MENTOR_BRIEF.md`.

## 먼저 읽을 것
- `README.md` — 실행법, 코드 구조, 담당 경계
- `docs/MENTOR_BRIEF.md` — 왜 이렇게 설계했는지, Gemini vs 로컬 비교 결과
- `server/config.py` — 모든 설정과 조건(condition) 정의가 여기 한 곳

## 환경
- Python 3.14 (WhisperX 미지원 → faster-whisper 폴백이 코드에 있음). 가상환경 `.venv`
- Windows 한글 콘솔: 서버 띄우기 전 `$env:PYTHONUTF8 = "1"` (cp949 인코딩 오류 예방)
- 서버: `python -m uvicorn server.main:app --port 8000` — **`--reload` 금지** (로컬 Qdrant는 프로세스 하나만)
- `.env` 는 저장소에 없음. `.env.example` 복사 후 `GEMINI_API_KEY` 채우기
- `data/` (SQLite, Qdrant, 프레임·오디오) 는 저장소에 없음 — 새 컴퓨터에선 확장으로 쇼츠를 새로 저장해야 데이터가 생김
- 로컬 모델 가중치 약 6.3GB, 첫 실행 때 HF/GitHub에서 자동 다운로드. 로컬 모델 두 개 이상 동시 실행 금지 (16GB RAM에서 OOM)

## 설계 원칙 (바꾸지 말 것)
1. 서버는 유튜브에 접속하지 않는다. 프레임·오디오는 확장이 브라우저에서 뽑아 올린다
2. 모델 단계마다 `gemini` 대체품과 로컬 백엔드를 **같은 인터페이스**로 둔다. 전환은 `.env` 한 줄 또는 `use_condition()`
3. `DEVICE` 는 `config.py` 한 곳. 코드에 `"cuda"` 직접 쓰지 않는다
4. Gemini 호출은 영상당 `MAX_GEMINI_CALLS_PER_VIDEO`(3) 를 코드에서 강제
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
```

## 코드 스타일
- 주석·문서·커밋 메시지는 한국어. 파일 첫 docstring에 "단계 N · 무엇. 담당 X" 형식
- 커밋 제목은 `feat:` / `fix:` 접두사 + 한국어 한 줄, 본문에 왜 바꿨는지
- 파일 끝 줄바꿈은 LF

## 지금 상태 / 다음 할 일
- 전 구간 Gemini 조건으로 동작, 로컬 4개 모델(faster-whisper·EasyOCR·BGE-M3·SigLIP2) CPU에서 확인 완료
- 다음: OCR 프레임 수 줄이기(4초 카드당 1~2장), `DEVICE=cuda` 재측정, 정보원별 기여도 실험 — 자세한 건 `docs/MENTOR_BRIEF.md` 10장
