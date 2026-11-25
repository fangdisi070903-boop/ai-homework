"""出院随访与患者协作 · MVP（FastAPI）

接口一览：
  POST /auth/login                  登录，拿 token
  GET  /me                          当前用户与权限点
  GET  /followups                   随访任务列表（按角色/科室/本人收敛）
  GET  /followups/{id}              任务详情
  POST /followups/plan              按病种生成随访计划（REQ-01，医生/管理员）
  POST /followups/{id}/records      录入随访结果（REQ-02/06，护士/医生/管理员）
  GET  /stats/completion            随访完成率（REQ-04，单一质控口径）
  POST /qa                          患者出院指导问答（REQ-03，带出处/转人工）
  GET  /admin/users                 用户清单（REQ-05，仅管理员）
  GET  /admin/audit                 审计日志（REQ-06，仅管理员）
  GET  /health                      健康检查

运行：cd mvp && uvicorn app.main:app --reload
"""
from datetime import datetime
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import audit, auth, rules, store
from .rag import get_service

app = FastAPI(
    title="出院随访与患者协作 MVP",
    description="FDE 训练项目 · 清禾综合医院（课程模拟，数据虚构）",
    version="0.1.0",
)


# ---------- 认证 ----------
class LoginIn(BaseModel):
    username: str
    password: str


def current_user(authorization: Optional[str] = Header(default=None)) -> dict:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    user = auth.user_by_token(token)
    if not user:
        audit.log("anonymous", "auth", "-", "deny", "缺少或无效 token")
        raise HTTPException(status_code=401, detail="未登录或 token 无效")
    return user


@app.post("/auth/login")
def login(payload: LoginIn):
    token = auth.login(payload.username, payload.password)
    if not token:
        audit.log(payload.username, "login", "-", "deny", "账号或密码错误")
        raise HTTPException(status_code=401, detail="账号或密码错误")
    user = auth.user_by_token(token)
    audit.log(payload.username, "login", "-", "allow", "角色=" + user["role"])
    return {
        "token": token,
        "username": payload.username,
        "name": user["name"],
        "role": user["role"],
        "role_name": auth.ROLE_NAMES[user["role"]],
        "permissions": auth.PERMISSIONS[user["role"]],
    }


@app.get("/me")
def me(user: dict = Depends(current_user)):
    return {
        "username": user["username"],
        "name": user["name"],
        "role": user["role"],
        "role_name": auth.ROLE_NAMES[user["role"]],
        "dept": user.get("dept", ""),
        "permissions": auth.PERMISSIONS[user["role"]],
    }


# ---------- 随访任务 ----------
@app.get("/followups")
def list_followups(user: dict = Depends(current_user)):
    if not (
        auth.can(user, "followups:read:all")
        or auth.can(user, "followups:read:dept")
        or auth.can(user, "followups:read:self")
    ):
        raise HTTPException(status_code=403, detail="无权查看随访任务")

    items = [f for f in store.load_followups() if auth.can_read_followup(user, f)]
    if user["role"] == "patient":
        items = [auth.mask_for_patient(f) for f in items]
    return {"count": len(items), "items": items}


@app.get("/followups/{followup_id}")
def get_followup(followup_id: str, user: dict = Depends(current_user)):
    try:
        item = store.find_followup(followup_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="随访任务不存在")
    if not auth.can_read_followup(user, item):
        audit.log(user["username"], "read_followup", followup_id, "deny", "越权访问")
        raise HTTPException(status_code=403, detail="无权访问该患者数据")
    if user["role"] == "patient":
        return auth.mask_for_patient(item)
    return item


class PlanIn(BaseModel):
    disease: str
    discharge_date: str = Field(..., description="YYYY-MM-DD")
    patient_mrn: Optional[str] = None
    dept: Optional[str] = None


@app.post("/followups/plan")
def create_plan(payload: PlanIn, user: dict = Depends(current_user)):
    if not (auth.can(user, "followups:plan") or auth.can(user, "followups:write:all")):
        audit.log(user["username"], "create_plan", payload.disease, "deny", "无计划生成权限")
        raise HTTPException(status_code=403, detail="无权生成随访计划")
    try:
        plan = rules.build_plan(payload.discharge_date, payload.disease)
    except ValueError:
        raise HTTPException(status_code=400, detail="discharge_date 格式应为 YYYY-MM-DD")
    audit.log(user["username"], "create_plan", payload.disease, "allow", "risk=" + plan["risk_level"])
    return plan


class RecordIn(BaseModel):
    connected: bool
    symptoms: str = ""
    adherence: str = ""
    followup_action: str = ""
    note: str = ""
    request_id: str = Field(..., description="幂等键，同一请求重复提交只生效一次")


@app.post("/followups/{followup_id}/records")
def add_record(followup_id: str, payload: RecordIn, user: dict = Depends(current_user)):
    if not (
        auth.can(user, "followups:record")
        or auth.can(user, "followups:write:all")
    ):
        audit.log(user["username"], "add_record", followup_id, "deny", "无记录权限")
        raise HTTPException(status_code=403, detail="无权录入随访记录")
    try:
        item = store.find_followup(followup_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="随访任务不存在")
    if not auth.can_read_followup(user, item):
        audit.log(user["username"], "add_record", followup_id, "deny", "跨科室写入")
        raise HTTPException(status_code=403, detail="无权操作该患者数据")

    # REQ-02 异常处理：未接通必须写明后续动作
    if not payload.connected and not payload.followup_action.strip():
        raise HTTPException(
            status_code=400,
            detail="未接通时必须填写后续动作（重拨 / 短信 / 转人工指派）",
        )

    result = {}

    def mutate(target: dict) -> None:
        request_ids = target.setdefault("request_ids", [])
        if payload.request_id in request_ids:
            result["idempotent"] = True
            result["record"] = target["records"][-1]
            return
        revision = len(target.get("records", [])) + 1
        record = {
            "record_id": "%s-R%d" % (followup_id, revision),
            "at": datetime.now().isoformat(timespec="seconds"),
            "operator_id": user["username"],
            "operator": user["name"],
            "connected": payload.connected,
            "symptoms": payload.symptoms,
            "adherence": payload.adherence,
            "followup_action": payload.followup_action,
            "note": payload.note,
            "revision": revision,
        }
        target.setdefault("records", []).append(record)
        request_ids.append(payload.request_id)
        # 状态推进：接通=已完成；未接通但写了后续动作=待跟进（不标完成）
        target["status"] = "已完成" if payload.connected else "待跟进"
        result["idempotent"] = False
        result["record"] = record

    store.update_followup(followup_id, mutate)
    status = "idempotent" if result["idempotent"] else "created"
    audit.log(user["username"], "add_record", followup_id, "allow", status)
    return {
        "followup_id": followup_id,
        "idempotent": result["idempotent"],
        "record": result["record"],
        "status": store.find_followup(followup_id)["status"],
    }


# ---------- 统计（单一口径）----------
@app.get("/stats/completion")
def completion_stats(user: dict = Depends(current_user)):
    if not (auth.can(user, "stats:read:all") or auth.can(user, "stats:read:dept")):
        raise HTTPException(status_code=403, detail="无权查看统计")
    items = store.load_followups()
    if not auth.can(user, "stats:read:all"):
        items = [f for f in items if f.get("dept") == user.get("dept")]

    total = len(items)
    done = len([f for f in items if f["status"] == "已完成"])
    lost = len([f for f in items if f["status"] == "失访"])
    return {
        "scope": "全院" if auth.can(user, "stats:read:all") else user.get("dept"),
        "definition": "完成随访率 = 完成随访表例数 ÷ 应随访例数（质控办口径，EV-12）",
        "total": total,
        "completed": done,
        "lost": lost,
        "completion_rate": round(done / total * 100, 1) if total else 0.0,
    }


# ---------- 患者问答（RAG + 知识图谱）----------
class QaIn(BaseModel):
    question: str


@app.post("/qa")
def qa(payload: QaIn, user: dict = Depends(current_user)):
    if not auth.can(user, "qa:use"):
        raise HTTPException(status_code=403, detail="无问答权限")
    result = get_service().answer(payload.question)
    audit.log(
        user["username"], "qa", payload.question[:30],
        "need_human" if result["need_human"] else "answered",
        (result.get("reason") or "") + " confidence=" + str(result.get("confidence")),
    )
    return result


# ---------- 管理员 ----------
@app.get("/admin/users")
def admin_users(user: dict = Depends(current_user)):
    if not auth.can(user, "users:manage"):
        audit.log(user["username"], "admin_users", "-", "deny", "非管理员")
        raise HTTPException(status_code=403, detail="仅管理员可操作")
    users = auth.load_users()
    return {
        "users": [
            {
                "username": name,
                "name": info["name"],
                "role": info["role"],
                "role_name": auth.ROLE_NAMES[info["role"]],
                "dept": info.get("dept", ""),
                "tenant": info.get("tenant", ""),
            }
            for name, info in users.items()
        ]
    }


@app.get("/admin/audit")
def admin_audit(user: dict = Depends(current_user), limit: int = 50):
    if not auth.can(user, "audit:read"):
        raise HTTPException(status_code=403, detail="仅管理员可查看审计日志")
    return {"records": audit.read_all(limit)}


@app.get("/health")
def health():
    return {"status": "ok", "service": "followup-mvp", "kb_chunks": len(get_service().chunks)}
