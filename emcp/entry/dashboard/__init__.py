"""/dashboard 화면.

- reports.py: 보고를 보여 주기 전의 계산 (시간대, 그래프 값, 표의 줄, 못 보낸 보고 묶기)
- page.py: 템플릿에 값을 넣어 페이지를 그린다 (render)
- templates/: 마크업. page.html 이 페이지 전체, boards.html 이 보드 관리 칸
- static/: 스타일과 스크립트. 페이지에 그대로 들어간다
"""

from . import reports, page  # noqa: F401
