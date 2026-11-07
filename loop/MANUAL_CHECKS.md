# 수동 확인 항목

루프는 Fake Provider와 로컬 Docker로만 검증합니다. 아래는 실제 인프라·계정·파일이 필요해 사람이 확인해야 합니다. 결과에 따라 TRD를 고칩니다(TRD 13장).

| # | 항목 | 확인 방법 | 관련 |
| --- | --- | --- | --- |
| 1 | ParadeDB arm64 이미지에서 pg\_search + pgvector + RRF 쿼리 | OCI VM에서 `docker compose up` 후 검색 실행 | TRD 13-1, T09, T17 |
| 2 | 임베딩 실제 차원, 요청당 최대 입력 수, `modality` 값 | 실제 키로 한 번 호출, `EMBED_DIM` 확정 | TRD 13-2, T03 |
| 3 | NVIDIA API 지연·429 동작, 재정렬 엔드포인트, 추론 텍스트 출력 | 실제 키로 질의 20회, `EMBED_DAILY_CHUNK_LIMIT` 확정 | TRD 13-3, 7.4 |
| 4 | 내 HWP·HWPX 파일 10개 이상, XLSX 파싱 품질 | 샘플을 모은 뒤 `bash scripts/loop.sh --only T18`, 추출 결과를 원본과 비교 | TRD 13-4, T18(미룸) |
| 5 | OCI 프리 티어 유휴 회수 조건 | OCI 정책 확인 | TRD 13-5 |
| 6 | `tailscale serve` HTTPS, Secure 쿠키, Chrome·Edge에서 `chanjin-sync://run`·`reveal` | 실제 설치 후 버튼 동작 | TRD 13-6, T08, T11, T13 |
| 7 | 백업 → 새 DB 복구, PC 밖 개인키 사본으로 복호화 | `restore.sh` 실제 실행 | TRD 13-7, T14 |
| 8 | `rag-agent install` 실제 등록(작업 스케줄러 4회, 레지스트리) | 설치 후 예약 시각에 실행 기록 확인, `uninstall`로 원복 | TRD 10.1, T08 |
| 9 | 실제 모델로 평가셋 30~50문항 실행 | `rag-eval run`(NVIDIA Provider) | PRD 8장, T15 |
