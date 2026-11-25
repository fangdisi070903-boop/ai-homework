"""审计日志（REQ-06）。

每条关键操作写一行 JSONL：谁、何时、对什么做了什么、结果如何。
对应合规红线："修改必须留痕，不可覆盖删除"。
"""
import json
import os
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
AUDIT_FILE = os.path.join(DATA_DIR, "audit.jsonl")


def log(actor: str, action: str, target: str, result: str, detail: str = "") -> None:
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "actor": actor,
        "action": action,
        "target": target,
        "result": result,
        "detail": detail,
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_all(limit: int = 200):
    if not os.path.exists(AUDIT_FILE):
        return []
    with open(AUDIT_FILE, encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]
    return lines[-limit:]
