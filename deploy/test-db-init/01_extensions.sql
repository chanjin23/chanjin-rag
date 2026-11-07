-- 테스트 DB 확장 초기화. pg_search(BM25)와 pgvector(vector, halfvec)를 만든다 (TRD 2장).
CREATE EXTENSION IF NOT EXISTS pg_search;
CREATE EXTENSION IF NOT EXISTS vector;
