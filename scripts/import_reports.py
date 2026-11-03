"""옛 볼륨의 reports.jsonl 을 Neon 으로 옮긴다. 다시 돌려도 이미 들어간 보고는 건너뛴다.

    uv run --env-file .env python -m scripts.import_reports reports.jsonl

옛 파일의 한 줄은 지금 서버가 버퍼에 쓰는 모양과 이름만 다르다 (at → received_at, fw → fw_version).
at_estimated 는 쓰는 곳이 없어 옮기지 않는다. 원본(raw)과 날씨는 그때 저장하지 않았으므로 없다.
"""

import json
import sys

import db


def convert(old: dict) -> dict:
    row = {k: v for k, v in old.items() if k not in ("at", "fw", "at_estimated")}
    row["received_at"] = old["at"]
    if "fw" in old:
        row["fw_version"] = old["fw"]
    return row


def main(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        rows = [convert(json.loads(line)) for line in f if line.strip()]
    # 앞뒤 보고를 비교해 반복해서 실려 온 실패 기록을 거르므로, 나눠 넣지 않고 한 번에 넣는다.
    print(f"{path}: {len(rows)} rows -> {db.add_reports(rows)}")


if __name__ == "__main__":
    main(sys.argv[1])
