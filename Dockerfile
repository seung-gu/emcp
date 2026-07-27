# uv 가 포함된 공식 이미지 사용 (Python 3.12)
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# 의존성 먼저 설치 (레이어 캐시 활용)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 애플리케이션 코드
COPY server.py ./

ENV PORT=8080
EXPOSE 8080

CMD ["uv", "run", "--no-sync", "python", "server.py"]
