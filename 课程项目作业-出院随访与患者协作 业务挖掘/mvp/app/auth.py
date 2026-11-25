"""MVP：出院随访与患者协作 —— 权限分层（REQ-05）。

设计要点（对应 03-现场调研分析.md 4.2 人工控制点、REQ-05/REQ-06）：
- 四种角色：admin（SaaS 管理员）/ doctor（医生）/ nurse（随访护士）/ patient（患者）
- 最小权限：患者只能看自己的随访任务；医护只能看本科室；管理员可跨租户管理
- 所有拒绝都要能写审计日志，因此这里只做判定，不写日志（由 audit 负责）
"""
import json
import os
import secrets
from typing import Dict, List, Optional

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

ROLE_NAMES = {
    "admin": "SaaS 管理员",
    "doctor": "医生",
    "nurse": "随访护士",
    "patient": "患者",
}

# 权限点：动作:范围
PERMISSIONS: Dict[str, List[str]] = {
    "admin": [
        "followups:read:all", "followups:write:all", "stats:read:all",
        "qa:use", "users:manage", "audit:read", "tenant:manage",
    ],
    "doctor": [
        "followups:read:dept", "followups:plan", "followups:record",
        "stats:read:dept", "qa:use",
    ],
    "nurse": [
        "followups:read:dept", "followups:record", "stats:read:dept", "qa:use",
    ],
    "patient": [
        "followups:read:self", "qa:use",
    ],
}


def load_users() -> Dict[str, dict]:
    with open(os.path.join(DATA_DIR, "users.json"), encoding="utf-8") as f:
        return json.load(f)["users"]


def login(username: str, password: str) -> Optional[str]:
    """校验账号密码，返回 token；失败返回 None。"""
    users = load_users()
    user = users.get(username)
    if not user or user.get("password") != password:
        return None
    token = secrets.token_urlsafe(16)
    TOKENS[token] = username
    return token


# 演示用的内存 token 表；生产应替换为 JWT / 会话服务
TOKENS: Dict[str, str] = {}


def user_by_token(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    username = TOKENS.get(token)
    if not username:
        return None
    user = load_users().get(username)
    if not user:
        return None
    result = dict(user)
    result["username"] = username
    result.pop("password", None)
    return result


def can(user: dict, perm: str) -> bool:
    return perm in PERMISSIONS.get(user.get("role", ""), [])


def can_read_followup(user: dict, followup: dict) -> bool:
    """数据级权限：管理员全院、医护本科室、患者本人。"""
    role = user.get("role")
    if role == "admin":
        return True
    if role in ("doctor", "nurse"):
        return (
            user.get("tenant") == followup.get("tenant")
            and user.get("dept") == followup.get("dept")
        )
    if role == "patient":
        return user.get("mrn") == followup.get("mrn")
    return False


def mask_for_patient(followup: dict) -> dict:
    """患者视角脱敏：不返回记录人姓名等院内信息。"""
    item = dict(followup)
    item["records"] = [
        {k: v for k, v in r.items() if k not in ("operator", "operator_id")}
        for r in followup.get("records", [])
    ]
    item.pop("internal_note", None)
    return item
