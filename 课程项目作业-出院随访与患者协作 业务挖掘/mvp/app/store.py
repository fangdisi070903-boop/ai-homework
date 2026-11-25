"""JSON 文件存储层。

课程 MVP 用文件代替数据库：
- 便于 Harness 复现：每次评估前可把 followups.json 重置为种子数据
- 生产替换点：Database / ORM，接口保持不变
"""
import json
import os
import threading
from typing import Dict, List

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
FOLLOWUP_FILE = os.path.join(DATA_DIR, "followups.json")

_lock = threading.RLock()


def load_followups() -> List[dict]:
    with _lock:
        with open(FOLLOWUP_FILE, encoding="utf-8") as f:
            return json.load(f)["followups"]


def save_followups(followups: List[dict]) -> None:
    with _lock:
        with open(FOLLOWUP_FILE, "w", encoding="utf-8") as f:
            json.dump({"followups": followups}, f, ensure_ascii=False, indent=2)


def find_followup(followup_id: str) -> dict:
    for item in load_followups():
        if item["id"] == followup_id:
            return item
    raise KeyError(followup_id)


def update_followup(followup_id: str, mutator) -> dict:
    """读-改-写加锁，避免并发覆盖（REQ-06 防重复）。"""
    with _lock:
        followups = load_followups()
        for item in followups:
            if item["id"] == followup_id:
                mutator(item)
                save_followups(followups)
                return item
    raise KeyError(followup_id)
