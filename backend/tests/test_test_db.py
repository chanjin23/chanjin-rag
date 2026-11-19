"""테스트 DB(ParadeDB, deploy/compose.test.yaml)가 준비됐는지 확인한다."""

import psycopg

from app.core.config import get_settings


def test_extensions_available(db: psycopg.Connection[tuple[object, ...]]) -> None:
    rows = db.execute(
        "select extname from pg_extension where extname in ('pg_search', 'vector')"
    ).fetchall()
    assert {r[0] for r in rows} == {"pg_search", "vector"}

    # pg_search의 BM25 색인 방식과 pgvector의 halfvec(EMBED_DIM 차원, TRD 6.3)이 동작하는지
    am = db.execute("select 1 from pg_am where amname = 'bm25'").fetchone()
    assert am is not None

    dim = get_settings().embed_dim
    db.execute(f"create temp table t_embed (id int primary key, v halfvec({dim}))")
    db.execute("create index on t_embed using hnsw (v halfvec_cosine_ops)")
    vec = "[" + ",".join(["1"] * dim) + "]"
    db.execute("insert into t_embed values (1, %s::halfvec)", (vec,))
    dist = db.execute("select v <=> %s::halfvec from t_embed", (vec,)).fetchone()
    assert dist is not None
    assert abs(float(str(dist[0]))) < 1e-3


def test_db_fixture_rolls_back(db: psycopg.Connection[tuple[object, ...]]) -> None:
    # 앞 테스트에서 만든 임시 테이블은 롤백으로 사라졌어야 한다
    found = db.execute("select to_regclass('pg_temp.t_embed')").fetchone()
    assert found is not None
    assert found[0] is None
