"""Harness：用固定评估集验证 MVP 行为（阶段 06 测试评估）。

用法：
    cd mvp
    python harness/run_eval.py

输出：
    harness/report.json   机器可读结果
    控制台                 人类可读汇总

说明：
- 每次运行前备份 data/followups.json，结束后恢复，保证仓库数据不被评估污染；
- 结果属于「模拟验证」（接口层调用，非真实 HIS 接入，非真实用户验收），报告中如实标注。
"""
import json
import os
import shutil
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

CASES_FILE = os.path.join(BASE_DIR, "harness", "cases.json")
REPORT_FILE = os.path.join(BASE_DIR, "harness", "report.json")
USERS_FILE = os.path.join(BASE_DIR, "data", "users.json")
FOLLOWUP_FILE = os.path.join(BASE_DIR, "data", "followups.json")
BACKUP_FILE = FOLLOWUP_FILE + ".bak"


def login(client, username, password):
    resp = client.post("/auth/login", json={"username": username, "password": password})
    resp.raise_for_status()
    return resp.json()["token"]


def auth_headers(token):
    return {"Authorization": "Bearer " + token}


def main():
    if not os.path.exists(CASES_FILE):
        print("找不到评估集:", CASES_FILE)
        return 2

    users = json.load(open(USERS_FILE, encoding="utf-8"))["users"]
    passwords = {name: info["password"] for name, info in users.items()}
    cases = json.load(open(CASES_FILE, encoding="utf-8"))

    shutil.copyfile(FOLLOWUP_FILE, BACKUP_FILE)
    results = []
    try:
        client = TestClient(app)
        tokens = {}

        def token_of(name):
            if name not in tokens:
                tokens[name] = login(client, name, passwords[name])
            return tokens[name]

        print("=" * 78)
        print("Harness 评估报告 |", datetime.now().isoformat(timespec="seconds"))
        print("验证类型：模拟验证（接口层），非真实模型调用、非业务验收")
        print("=" * 78)

        # 1. 检索正确率（REQ-03）
        for case in cases["retrieval"]:
            resp = client.post("/qa", json={"question": case["question"]},
                               headers=auth_headers(token_of("patient_li")))
            data = resp.json()
            docs = [s["doc"] for s in data.get("sources", [])]
            ok = (not data.get("need_human")) and case["expect_doc"] in docs
            results.append({
                "category": "retrieval", "id": case["id"], "pass": ok,
                "question": case["question"],
                "expect_doc": case["expect_doc"], "got_docs": docs,
                "need_human": data.get("need_human"),
            })
            print("[检索] %s %s 期望=%s 实际=%s" % (
                "PASS" if ok else "FAIL", case["id"], case["expect_doc"], docs[:1]))

        # 2. 越界与医疗红线（REQ-03 必须拒绝）
        for case in cases["boundary"]:
            resp = client.post("/qa", json={"question": case["question"]},
                               headers=auth_headers(token_of("patient_li")))
            data = resp.json()
            ok = data.get("need_human") is True and data.get("reason") == case["expect_reason"]
            results.append({
                "category": "boundary", "id": case["id"], "pass": ok,
                "question": case["question"], "expect_reason": case["expect_reason"],
                "got_reason": data.get("reason"), "got_sources": data.get("sources"),
            })
            print("[边界] %s %s 期望=%s 实际=%s" % (
                "PASS" if ok else "FAIL", case["id"], case["expect_reason"], data.get("reason")))

        # 3. 权限（REQ-05）
        for case in cases["permission"]:
            resp = client.request(case["method"], case["path"],
                                  headers=auth_headers(token_of(case["user"])))
            ok = resp.status_code == case["expect"]
            detail = ""
            if case["id"] == "P-06" and resp.status_code == 200:
                payload = resp.json()
                ok = payload.get("count") == 1
                detail = "患者可见条数=%s" % payload.get("count")
            results.append({
                "category": "permission", "id": case["id"], "pass": ok,
                "user": case["user"], "path": case["path"],
                "expect": case["expect"], "got": resp.status_code, "detail": detail,
            })
            print("[权限] %s %s %s %s -> 期望 %s 实际 %s %s" % (
                "PASS" if ok else "FAIL", case["id"], case["user"], case["path"],
                case["expect"], resp.status_code, detail))

        # 4. 分层规则（REQ-01）
        for case in cases["plan"]:
            resp = client.post("/followups/plan", json={
                "disease": case["disease"], "discharge_date": case["discharge_date"],
            }, headers=auth_headers(token_of("doctor_wang")))
            data = resp.json()
            ok = data.get("risk_level") == case["expect_risk"]
            if "expect_first_due" in case:
                ok = ok and data.get("plan", [{}])[0].get("due_date") == case["expect_first_due"]
            if case.get("expect_needs_review"):
                ok = ok and data.get("needs_review") is True
            results.append({
                "category": "plan", "id": case["id"], "pass": ok,
                "disease": case["disease"], "expect_risk": case["expect_risk"],
                "got_risk": data.get("risk_level"),
                "first_due": data.get("plan", [{}])[0].get("due_date"),
                "needs_review": data.get("needs_review"),
            })
            print("[规则] %s %s %s -> 风险=%s 首访=%s 待确认=%s" % (
                "PASS" if ok else "FAIL", case["id"], case["disease"],
                data.get("risk_level"), data.get("plan", [{}])[0].get("due_date"),
                data.get("needs_review")))

        # 5. 幂等（REQ-06）
        idem = cases["idempotency"]
        body = {
            "connected": True, "symptoms": "无不适", "adherence": "依从",
            "followup_action": "", "note": "harness", "request_id": idem["request_id"],
        }
        headers = auth_headers(token_of(idem["user"]))
        first = client.post("/followups/%s/records" % idem["followup_id"], json=body, headers=headers)
        second = client.post("/followups/%s/records" % idem["followup_id"], json=body, headers=headers)
        now = client.get("/followups/%s" % idem["followup_id"], headers=headers).json()
        ok = (first.status_code == 200 and second.status_code == 200
              and second.json().get("idempotent") is True and len(now.get("records", [])) == 1)
        results.append({
            "category": "idempotency", "id": idem["id"], "pass": ok,
            "first_idempotent": first.json().get("idempotent"),
            "second_idempotent": second.json().get("idempotent"),
            "record_count": len(now.get("records", [])),
        })
        print("[幂等] %s 第二次 idempotent=%s 记录数=%s" % (
            "PASS" if ok else "FAIL", second.json().get("idempotent"),
            len(now.get("records", []))))

        # 6. 未接通必须填写后续动作（REQ-02 异常处理）
        bad_body = {**body, "connected": False, "followup_action": "",
                    "request_id": idem["request_id"] + "-bad"}
        bad = client.post("/followups/FU-202610-012/records", json=bad_body,
                          headers=auth_headers(token_of("nurse_zhou")))
        ok = bad.status_code == 400
        results.append({
            "category": "exception", "id": "E-01", "pass": ok,
            "expect": 400, "got": bad.status_code,
        })
        print("[异常] %s 未接通未填后续动作 -> 期望 400 实际 %s" % (
            "PASS" if ok else "FAIL", bad.status_code))

    finally:
        shutil.move(BACKUP_FILE, FOLLOWUP_FILE)

    # 汇总
    summary = {}
    for item in results:
        bucket = summary.setdefault(item["category"], {"total": 0, "pass": 0})
        bucket["total"] += 1
        bucket["pass"] += 1 if item["pass"] else 0
    for bucket in summary.values():
        bucket["rate"] = round(bucket["pass"] / bucket["total"] * 100, 1) if bucket["total"] else 0.0

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "verification_type": "模拟验证（接口层）",
        "not_covered": ["真实 HIS 接口联调", "真实临床模型调用", "真实用户 UAT"],
        "summary": summary,
        "details": results,
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("-" * 78)
    for name, bucket in summary.items():
        print("%-12s %d/%d  %s%%" % (name, bucket["pass"], bucket["total"], bucket["rate"]))
    print("报告已写入:", REPORT_FILE)

    critical = ["retrieval", "boundary", "permission", "plan", "idempotency", "exception"]
    failed = [r for r in results if not r["pass"]]
    print("=" * 78)
    print("总体结论:", "全部通过" if not failed else "存在 %d 项失败" % len(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
