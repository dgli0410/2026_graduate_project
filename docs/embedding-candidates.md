# 글 좌표(텍스트 임베딩) 모델 후보

> 2026-10-08 기준. "왜 이 임베딩 모델인가"에 답하기 위한 후보 선정 근거와 비교 방법입니다.
> 최종 선택은 우리 평가셋 결과로 합니다. 아래 공개 수치는 **후보를 고르는 근거**로만 씁니다.

## 후보

| 이름 | 모델 | 크기 · 차원 | 접두어 (질문 / 장면) | 라이선스 | 넣은 이유 |
| --- | --- | --- | --- | --- | --- |
| `bge` | BAAI/bge-m3 | 568M · 1024 | 없음 | MIT | 현재 로컬 기본값(기준) |
| `kure` | nlpai-lab/KURE-v1 | 568M · 1024 | 없음 | MIT | bge-m3 를 한국어 검색으로 미세조정. 비용이 bge-m3 와 같음 |
| `arctic` | dragonkue/snowflake-arctic-embed-l-v2.0-ko | 568M · 1024 | `query: ` / 없음 | Apache-2.0 | 노트북에서 돌아가는 단일 벡터 모델 중 공개 한국어 벤치마크 최상위 |
| `e5small` | dragonkue/multilingual-e5-small-ko-v2 | 118M · 384 | `query: ` / `passage: ` | Apache-2.0 | 크기 1/5. 큰 모델이 짧은 장면 카드에서 실제로 얼마나 더 나은지 보는 기준 |
| `gemini` | gemini-embedding-001 (768차원) | API | 작업 종류(task_type)로 구분 | API 약관 | 현재 코드 기본값. API 대 로컬 비교 기준 |

접두어는 모델 카드에 적힌 그대로입니다. 빠뜨리면 오류 없이 점수만 떨어집니다. 설정은 `server/config.py` 의 `EMBED_CANDIDATES`.

## 공개 한국어 검색 벤치마크

| 모델 | ko-embedding-leaderboard ¹ | MTEB 한국어 검색 9종 ² |
| --- | --- | --- |
| bge-m3 | 79.30 | 0.751 |
| KURE-v1 | 80.76 | 0.762 |
| arctic-l-v2.0-ko | **82.14** | 0.765 |
| e5-small-ko-v2 | 없음 (제작자 카드 7종 평균 NDCG@10 0.693, 같은 표의 bge-m3 0.724) | 없음 |
| gemini-embedding-001 | 없음 | 없음 ³ |

1. [OnAnd0n/ko-embedding-leaderboard](https://github.com/OnAnd0n/ko-embedding-leaderboard) — 제작사와 무관한 한국어 리더보드.
   한국어 검색 7종(Ko-StrategyQA, AutoRAG, PublicHealthQA 등), NDCG@5 · @10 평균.
2. [KURE-v2 모델 카드](https://huggingface.co/nlpai-lab/KURE-v2) — MTEB(kor, v2) 9종 평균 NDCG@10. 제작자 표.
3. Google 이 낸 수치는 다국어 MMTEB 평균(68.3)과 한국어 질문 → 영어 문서 검색(XOR-Retrieve)뿐입니다.
   한국어 문서 검색 수치가 없어 같은 칸에 넣지 않습니다.

출처마다 데이터 묶음이 달라 **같은 출처 안의 순위만** 의미가 있습니다.

## 넣지 않은 모델

- **점수가 bge-m3 이하:** KoE5, multilingual-e5-large(-instruct), gte-multilingual-base, Qwen3-Embedding-0.6B,
  harrier-oss-0.6b, EmbeddingGemma-300m. (위 리더보드 기준)
- **라이선스:** jina-embeddings v3 · v5 는 비상업(CC-BY-NC). EmbeddingGemma 는 사용 동의가 필요한 Gemma 약관.
- **CPU 노트북(16GB)에 너무 큼:** Qwen3-Embedding-4B · 8B, pplx-embed-4b, bge-multilingual-gemma2, comsat-embed-ko-8b.
- **나중 후보:** nlpai-lab/KURE-v2 (154M, MTEB 한국어 0.816 으로 가장 높음). 토큰마다 벡터를 만드는 방식이라
  창고 구조(멀티벡터)를 바꿔야 해서 이번 비교에서는 뺐습니다.

## 공개 수치만으로 고르지 않는 이유

공개 벤치마크는 정돈된 질문과 긴 문서입니다. 우리 장면 카드는 4초 분량의 짧은 글(음성 · 화면 자막 · 장면 설명)이고
자막에는 오인식이 섞입니다. 상위 모델끼리 0.01~0.02 차이는 우리 데이터에서 뒤집힐 수 있습니다.

## 비교 방법

같은 장면 카드를 후보 모델로 다시 색인해 후보마다 따로 창고에 넣고, 같은 질문으로 검색합니다.
장면 카드의 글과 그림 좌표는 모든 후보가 같으므로 차이는 글 좌표 모델에서만 나옵니다.
Gemini 생성 호출(캡션 · 음성 · 요약)은 하지 않습니다.

```powershell
# 서버를 끄고
python -m tools.build_embed_index                      # 후보와 준비 상태
python -m tools.build_embed_index --model all --yes    # 로컬 후보 전부 색인 (gemini 는 --model gemini 로 따로)
```

검색 (`/api/search` 와 같은 모양으로 돌려줍니다. 답변 생성은 기본으로 꺼져 있습니다):

```python
from server import embed_compare
embed_compare.search(user_id, "두부 언제 넣어?", "kure", top_k=5)
```

```
POST /api/embed-compare/search   {"query": "두부 언제 넣어?", "model": "kure", "top_k": 5}
GET  /api/embed-compare          후보마다 색인된 장면 수 (expected_segments 와 같아야 공정)
```

새로 저장한 영상은 후보 창고에 자동으로 들어가지 않습니다. 비교 전에 색인 도구를 다시 돌리세요.

## 결과 (평가셋 측정 후 채움)

| 모델 | nDCG@5 | Hit@1 | Recall@5 | bge 대비 | 색인 시간 | 질문 1개 |
| --- | --- | --- | --- | --- | --- | --- |
| bge | | | | – | | |
| kure | | | | | | |
| arctic | | | | | | |
| e5small | | | | | | |
| gemini | | | | | | |
