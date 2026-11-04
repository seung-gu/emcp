"""1층 입구. 바깥에서 들어오는 요청을 받는 곳이다. 서로 부르지 않고, 아래 층(service, state, infra)만 부른다.

- board_api.py: 보드와 바깥 서비스(Fly, GitHub Actions)가 부르는 HTTP (/weather, /firmware, /health)
- web.py: 로그인과 대시보드 (/auth, /dashboard, /r)
- tools.py: Claude 가 부르는 MCP 도구 (/mcp)
- dashboard/: 대시보드 화면. web.py 가 그리고, tools.py 는 표시 이름 몇 개를 같이 쓴다
"""
