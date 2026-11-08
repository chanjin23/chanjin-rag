"""Sync API의 서버 쪽 처리 (TRD 6.1, 8.2).

- `runs`: 동기화 실행 기록(시작·하트비트·완료)과 하트비트가 끊긴 실행의 `abandoned` 처리
- `manifest`: Agent가 보낸 전체 목록을 `documents`와 비교하는 판정표
- `documents`: 이동(경로만 갱신)과 복사(sections·chunks·임베딩 복사)
- `upload`: 업로드 내용 수신(해시 재계산, 크기 상한), 임시 원본 저장
- `scope`: 수집 범위(허용 폴더, `sync_config` 루트·제외 패턴·최대 크기) 검증
"""
