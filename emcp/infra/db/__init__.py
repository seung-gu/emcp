"""Neon Postgres (DATABASE_URL 필요). 영역마다 파일 하나다.

- core: 연결, 마이그레이션 적용, 서버가 기억해 둘 값(app_state)
- migrations: 테이블 변경 목록
- reports: 보고 저장과 읽기
- boards: 보드, 누가 볼 수 있는지, 등록, 등록 해제, 예전 기록, 지운 계정
- sharing: 공유

테이블을 어떻게 나눴고 왜 그런지는 docs/data-structure.ko.md 에 있다. 쓰는 쪽은 `from emcp.infra import db` 로
부르고 `db.boards.list_devices(...)` 처럼 파일을 골라 쓴다. 동시에 들어온 요청이 끼어들면 안 되는 규칙은 함수
하나가 트랜잭션 하나로 처리한다.
"""

from . import migrations, core, boards, reports, sharing  # noqa: F401  (서로 부르는 순서대로)
