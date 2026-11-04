#!/bin/sh
# 테스트용 Postgres 를 Docker 로 띄운다. 운영 Neon 과 같은 18 버전이고, 이미 떠 있으면 그대로 둔다.
# 테스트는 이 DB 만 쓴다 (tests/conftest.py 가 localhost 가 아닌 주소를 거절한다).
#   sh scripts/test-db.sh && uv run pytest
set -e
docker start emcp-test-db >/dev/null 2>&1 || docker run -d --name emcp-test-db -p 54329:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=emcp_test postgres:18 >/dev/null
echo "test database: postgresql://postgres:postgres@localhost:54329/emcp_test"
