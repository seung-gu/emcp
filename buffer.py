"""보고 버퍼. 보드 보고를 받으면 볼륨 위 파일에 한 줄씩 쌓고, 한 시간에 한 번 Neon 으로 옮긴다.

보고마다 DB 에 쓰면 30분마다 잠든 DB 를 깨워서 보드 몇 대만으로 무료 한도를 넘는다. 파일에 먼저 쓰니 DB 가
멈춰도 보고는 남는다. Fly 에서는 볼륨 위를 가리킨다 (fly.toml 의 REPORTS_BUFFER).
언제 옮길지는 actions.py 가 정한다.
"""

import glob
import json
import os
import threading
import time

import db

PATH = os.getenv("REPORTS_BUFFER", "pending-reports.jsonl")
_flush_lock = threading.Lock()


def append(row: dict) -> None:
    with open(PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def seal() -> None:
    """지금까지 쌓인 버퍼를 봉인 파일로 넘긴다. 이벤트 루프에서만 부른다 — 보고를 덧붙이는 것도
    루프에서만 하므로, 이름을 바꾸는 사이에 한 줄이 끼어들 수 없다."""
    if os.path.exists(PATH):
        os.replace(PATH, f"{PATH}.{time.time_ns()}")


def flush_sealed() -> bool:
    """봉인된 파일을 오래된 것부터 Neon 에 넣고 지운다. 실패하면 그 파일을 남겨 다음 번에 다시
    넣고 False 를 돌려준다 — 이미 들어간 보고는 db.add_reports 가 건너뛰므로 두 번 넣어도 된다."""
    with _flush_lock:
        for path in sorted(glob.glob(glob.escape(PATH) + ".*")):
            with open(path, encoding="utf-8") as f:
                rows = [json.loads(line) for line in f if line.strip()]
            try:
                result = db.add_reports(rows)
            except Exception as e:
                print(f"flush failed, keeping {path}: {e!r}", flush=True)
                return False
            os.remove(path)
            print(f"flushed {path}: {result}", flush=True)
    return True
