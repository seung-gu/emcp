"""펌웨어 자동 업데이트(OTA). 펌웨어 저장소의 최신 GitHub 릴리스를 받아 두고, 그보다 옛 펌웨어로 보고한
보드에 새 버전을 알려 준다. 보드는 GET /firmware/<보드 종류>/<버전> 으로 받아 스스로 설치한다.

릴리스는 펌웨어 저장소에 v 로 시작하는 태그를 push 하면 GitHub Actions 가 만든다. 파일은 보드 종류마다
하나(<종류>.bin)와, 릴리스 커밋부터 첫 커밋까지의 SHA 목록(commits.txt)이다. GitHub API 대신 릴리스 파일만
받으므로 API 횟수 제한에 걸리지 않는다.

확인은 타이머가 10분마다 한다(actions.run_due_jobs). 보드 요청은 받아 둔 것만 읽어서 GitHub 을 기다리지 않는다.
"""

import re

import httpx

LATEST = "https://github.com/seung-gu/eink-weather-display/releases/latest/download/"
BOARDS = ("weather-xiao", "weather-supermini")   # 펌웨어 저장소 platformio.ini 의 env 이름
MIN_BATTERY_MV = 3700   # 이보다 낮으면 업데이트를 미룬다. 받는 도중에 전원이 꺼지지 않게

# 받아 둔 최신 릴리스. commit 은 릴리스 커밋의 SHA, older 는 그 조상 커밋들, files 는 보드 종류마다 펌웨어.
# 통째로 바꿔 끼우므로 읽는 쪽은 잠그지 않는다.
_release: dict | None = None


def refresh() -> None:
    """최신 릴리스를 확인하고, 바뀌었으면 펌웨어를 받아 둔다. 실패하면 예외를 내고 받아 둔 것을 그대로 쓴다."""
    global _release
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        r = client.get(LATEST + "commits.txt")
        if r.status_code == 404:              # 릴리스가 아직 없다
            _release = None
            return
        r.raise_for_status()
        commits = r.text.split()
        if not commits or not all(re.fullmatch(r"[0-9a-f]{40}", c) for c in commits):
            raise ValueError("commits.txt 가 SHA 목록이 아니다")
        if _release and _release["commit"] == commits[0]:
            return
        files = {}
        for board in BOARDS:
            f = client.get(LATEST + f"{board}.bin")
            f.raise_for_status()
            files[board] = f.content
        # 받는 사이에 다음 릴리스가 나왔으면 파일과 목록이 서로 다른 릴리스다. 다음 확인 때 다시 받는다.
        if client.get(LATEST + "commits.txt").text.split()[:1] != commits[:1]:
            return
    _release = {"commit": commits[0], "older": commits[1:], "files": files}
    print(f"firmware release {commits[0][:7]}: " + ", ".join(f"{b} {len(d)}B" for b, d in files.items()), flush=True)


def offer(board, version, battery_mv: int) -> str | None:
    """이 보드가 받아야 할 펌웨어 버전(릴리스 커밋의 SHA 40자). 받을 게 없으면 None.

    릴리스보다 옛 커밋으로 빌드한 보드만 받는다. 수정 중인 빌드(-dirty)나 릴리스에 아직 없는 커밋으로 직접
    플래싱한 보드는 건드리지 않는다. 보드의 버전은 짧은 SHA 라 앞부분으로 맞춰 본다.
    """
    rel = _release
    if rel is None or not isinstance(board, str) or board not in rel["files"] or battery_mv < MIN_BATTERY_MV:
        return None
    if not isinstance(version, str) or len(version) < 7:
        return None
    if any(c.startswith(version) for c in rel["older"]):
        return rel["commit"]
    return None


def binary(board: str, version: str) -> bytes | None:
    """받아 둔 펌웨어. 최신 릴리스의 것만 준다."""
    rel = _release
    if rel is None or version != rel["commit"]:
        return None
    return rel["files"].get(board)
