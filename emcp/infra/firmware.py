"""펌웨어 자동 업데이트(OTA). 펌웨어 저장소의 최신 GitHub 릴리스를 받아 두고, 자동 업데이트를 켠 보드가
그 릴리스가 아닌 빌드로 보고하면 새 버전을 알려 준다. 보드는 GET /firmware/<보드 종류>/<버전> 으로 받아
스스로 설치한다.

릴리스는 펌웨어 저장소에 v 로 시작하는 태그를 push 하면 GitHub Actions 가 만든다. 파일은 보드 종류마다
하나(<종류>.bin)와, 릴리스 커밋부터 첫 커밋까지의 SHA 목록(commits.txt)이다. 그 첫 줄이 릴리스 커밋이다.
GitHub API 대신 릴리스 파일만 받으므로 API 횟수 제한에 걸리지 않는다. commits.txt 만 latest 주소로 받고,
그 주소가 넘겨 주는 주소(/releases/download/<태그>/…)에서 태그를 읽어 펌웨어는 그 태그 주소로 받는다. latest
주소는 파일마다 따로 넘어가서, 릴리스가 막 나온 직후에는 한 파일만 예전 릴리스를 가리킬 수 있다(v1.2.0 때
commits.txt 는 새 릴리스, 펌웨어는 v1.1.1 것을 받아 두어 보드들이 옛 펌웨어를 두 번 다시 설치했다).

GitHub 에 주기적으로 묻지 않는다. 받는 때는 둘뿐이다. 릴리스 워크플로가 릴리스를 만든 직후 POST
/firmware/refresh 로 알릴 때, 그리고 서버가 켜질 때 한 번(받아 둔 것이 메모리에만 있어서 재시작하면 비므로.
actions.run_due_jobs). 보드 요청은 받아 둔 것만 읽어서 GitHub 을 기다리지 않는다.
"""

import re

import httpx

RELEASES = "https://github.com/seung-gu/eink-weather-display/releases/"
LATEST = RELEASES + "latest/download/"
# 릴리스에 올리는 보드 종류. 펌웨어 저장소 platformio.ini 의 env 이름이고, 그 저장소 release.yml 이 빌드하는
# 목록과 같아야 한다. Super Mini(weather-supermini)는 쓰는 보드가 없어서 뺐다.
BOARDS = ("weather-xiao",)
MIN_BATTERY_MV = 3700   # 이보다 낮으면 업데이트를 미룬다. 받는 도중에 전원이 꺼지지 않게

# 받아 둔 최신 릴리스. commit 은 릴리스 커밋의 SHA, tag 는 태그 이름(v1.2.0), files 는
# 보드 종류마다 펌웨어. 통째로 바꿔 끼우므로 읽는 쪽은 잠그지 않는다.
_release: dict | None = None
_loaded = False   # 켜진 뒤 GitHub 에서 확인을 마쳤나. 릴리스가 없다는 답도 확인이다


def refresh() -> None:
    """최신 릴리스를 확인하고, 바뀌었으면 펌웨어를 받아 둔다. 실패하면 예외를 내고 받아 둔 것을 그대로 쓴다."""
    global _release, _loaded
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        r = client.get(LATEST + "commits.txt")
        if r.status_code == 404:              # 릴리스가 아직 없다
            _release, _loaded = None, True
            return
        r.raise_for_status()
        commits = r.text.split()
        if not commits or not all(re.fullmatch(r"[0-9a-f]{40}", c) for c in commits):
            raise ValueError("commits.txt 가 SHA 목록이 아니다")
        if _release and _release["commit"] == commits[0]:
            _loaded = True
            return
        tag = next((m.group(1) for h in (*r.history, r)
                    if (m := re.search(r"/releases/download/([^/]+)/commits\.txt$", h.url.path))), None)
        if tag is None:
            raise ValueError("latest 주소가 릴리스 태그 주소로 넘어가지 않았다")
        files = {}
        for board in BOARDS:
            f = client.get(RELEASES + f"download/{tag}/{board}.bin")
            f.raise_for_status()
            # 펌웨어는 자기 커밋(짧은 SHA)을 글자로 품고 있다(펌웨어의 FW_COMMIT). 없으면 다른 릴리스의 파일이다.
            if commits[0][:7].encode() not in f.content:
                raise ValueError(f"{tag} 의 {board}.bin 에 릴리스 커밋 {commits[0][:7]} 이 없다")
            files[board] = f.content
    _release, _loaded = {"commit": commits[0], "tag": tag, "files": files}, True
    print(f"firmware release {tag} {commits[0][:7]}: " + ", ".join(f"{b} {len(d)}B" for b, d in files.items()),
          flush=True)


def loaded() -> bool:
    return _loaded


def current() -> str | None:
    """받아 둔 릴리스의 커밋. 릴리스가 없으면 None."""
    rel = _release
    return rel["commit"] if rel else None


def latest_version() -> str | None:
    """받아 둔 릴리스의 태그 이름. 대시보드가 보드의 버전 옆에 보여 준다. 릴리스가 없으면 None."""
    rel = _release
    return rel["tag"] if rel else None


def runs_latest(version) -> bool | None:
    """보드가 보고한 버전(태그 이름이나 커밋)이 받아 둔 릴리스인가. 보드의 커밋은 짧은 SHA 라 앞부분으로 맞춰
    본다. -dirty 가 붙은 빌드는 릴리스 커밋이어도 릴리스가 아니다. 릴리스가 없으면 None."""
    rel = _release
    if rel is None:
        return None
    if not isinstance(version, str):
        return False
    return version == rel["tag"] or (len(version) >= 7 and rel["commit"].startswith(version))


def offer(board, version, battery_mv: int, auto_update: bool) -> str | None:
    """이 보드가 받아야 할 펌웨어 버전(릴리스 커밋의 SHA 40자). 받을 게 없으면 None.

    자동 업데이트를 켠 보드는 받아 둔 릴리스가 아니면 무엇을 돌리든(예전 릴리스, 테스트 빌드, -dirty 빌드)
    그 릴리스를 받는다. 테스트 빌드를 USB 로 올릴 때는 주인이 자동 업데이트를 꺼서 지킨다.
    """
    rel = _release
    if not auto_update or rel is None or not isinstance(board, str) or board not in rel["files"]:
        return None
    if battery_mv < MIN_BATTERY_MV or not isinstance(version, str) or len(version) < 7 or runs_latest(version):
        return None
    return rel["commit"]


def binary(board: str, version: str) -> bytes | None:
    """받아 둔 펌웨어. 최신 릴리스의 것만 준다."""
    rel = _release
    if rel is None or version != rel["commit"]:
        return None
    return rel["files"].get(board)
