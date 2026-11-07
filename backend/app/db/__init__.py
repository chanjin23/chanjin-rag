"""SQLAlchemy 모델, 세션, Hybrid 검색 SQL (TRD 5장, 7.2). Alembic 마이그레이션은 backend/alembic.

- `models`: TRD 5장의 테이블. 상세 DDL의 기준은 Alembic 마이그레이션이다.
- `engine`: async 엔진과 세션 팩토리(psycopg 3).
- `migrate`: `rag-backend migrate`가 쓰는 Alembic 실행 함수.
"""
