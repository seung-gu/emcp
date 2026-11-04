"""emcp: e-ink 날씨 보드 서버. 폴더 하나가 층 하나이고, 위 층만 아래 층을 import 한다.

- server.py: 조립. 입구의 경로와 도구를 앱 하나로 묶어 실행한다 (python -m emcp.server)
- entry/: 1층 입구. 보드 HTTP, 로그인과 대시보드, Claude 의 MCP 도구. 입구끼리는 서로 부르지 않는다
- service/: 2층 입구 여럿이 같이 쓰는 동작과 입력 검사
- state/: 3층 서버 메모리. 보드 사본과 보고 버퍼
- infra/: 4층 바깥. DB(Neon), 로그인(WorkOS), 날씨(open-meteo), 펌웨어 릴리스(GitHub)
"""
