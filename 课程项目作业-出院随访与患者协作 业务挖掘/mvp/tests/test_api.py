"""最小回归测试（pytest）。

运行： cd mvp && pytest -q
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def _login(client, username, password):
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": "Bearer " + resp.json()["token"]}


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["kb_chunks"] > 0


def test_login_and_me(client):
    headers = _login(client, "nurse_zhou", "nurse123")
    me = client.get("/me", headers=headers).json()
    assert me["role"] == "nurse"
    assert me["dept"] == "心内科"


def test_patient_sees_only_self(client):
    headers = _login(client, "patient_li", "pat123")
    data = client.get("/followups", headers=headers).json()
    assert data["count"] == 1
    assert data["items"][0]["mrn"] == "MRN20260077"


def test_cross_dept_denied(client):
    headers = _login(client, "nurse_zhou", "nurse123")
    resp = client.get("/followups/FU-202610-002", headers=headers)
    assert resp.status_code == 403


def test_qa_has_source(client):
    headers = _login(client, "patient_li", "pat123")
    data = client.post("/qa", json={"question": "心衰患者出院后多久做第一次随访？"},
                       headers=headers).json()
    assert data["need_human"] is False
    assert data["sources"][0]["doc"] == "01-心力衰竭出院指导.md"


def test_qa_refuses_medication(client):
    headers = _login(client, "patient_li", "pat123")
    data = client.post("/qa", json={"question": "呋塞米每天吃几片？"}, headers=headers).json()
    assert data["need_human"] is True
    assert data["reason"] == "medical_safety"


def test_plan_high_risk(client):
    headers = _login(client, "doctor_wang", "doc123")
    data = client.post("/followups/plan",
                       json={"disease": "COPD", "discharge_date": "2026-11-01"},
                       headers=headers).json()
    assert data["risk_level"] == "high"
    assert data["plan"][0]["due_date"] == "2026-11-08"
