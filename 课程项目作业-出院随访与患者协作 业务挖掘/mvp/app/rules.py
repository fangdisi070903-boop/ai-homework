"""随访分层规则（REQ-01）。

规则来源：《出院随访管理制度 V2.3》第 3 章。
原则（阶段 03 4.2）：规则只给建议，每日由护士长复核，规则不替代临床判断。
"""
from datetime import date, timedelta
from typing import Dict, List

# risk: high / medium / normal；offsets: 出院后第 N 天随访；checks: 该病种必查项
DISEASE_RULES: Dict[str, dict] = {
    "心力衰竭": {
        "risk": "high", "offsets": [7, 14, 30],
        "checks": ["每日体重", "尿量", "下肢水肿", "夜间能否平卧", "用药依从性"],
    },
    "COPD": {
        "risk": "high", "offsets": [7, 14, 30],
        "checks": ["气促程度", "吸入装置使用", "咳嗽咳痰", "用药依从性"],
    },
    "糖尿病足": {
        "risk": "high", "offsets": [7, 14, 30],
        "checks": ["足部溃疡", "换药情况", "血糖监测", "饮食控制"],
    },
    "PCI术后": {
        "risk": "high", "offsets": [7, 14, 30],
        "checks": ["穿刺点", "胸痛发作", "抗血小板用药依从性", "复诊安排"],
    },
    "脑卒中恢复期": {
        "risk": "high", "offsets": [7, 14, 30],
        "checks": ["肢体活动", "吞咽", "血压", "康复训练"],
    },
    "高血压": {
        "risk": "medium", "offsets": [14, 30],
        "checks": ["家庭血压监测", "头晕", "用药依从性"],
    },
    "2型糖尿病": {
        "risk": "medium", "offsets": [14, 30],
        "checks": ["血糖监测", "饮食", "足部皮肤"],
    },
    "冠心病": {
        "risk": "medium", "offsets": [14, 30],
        "checks": ["胸痛", "活动耐量", "用药依从性"],
    },
}

DEFAULT_RULE = {
    "risk": "normal", "offsets": [30],
    "checks": ["症状变化", "用药依从性", "复诊安排"],
}


def build_plan(discharge_date: str, disease: str) -> dict:
    """按病种生成随访计划。

    未知病种 → 归"常规"并标记 needs_review=True（异常处理，不让系统猜）。
    """
    rule = DISEASE_RULES.get(disease)
    needs_review = rule is None
    rule = rule or DEFAULT_RULE
    start = date.fromisoformat(discharge_date)
    items: List[dict] = []
    for index, offset in enumerate(rule["offsets"], start=1):
        items.append({
            "seq": index,
            "due_date": (start + timedelta(days=offset)).isoformat(),
            "type": "首次随访" if index == 1 else "后续随访",
            "required": True,
            "checks": list(rule["checks"]),
        })
    return {
        "disease": disease,
        "risk_level": rule["risk"],
        "needs_review": needs_review,
        "review_reason": "病种不在随访目录内，已按常规处理，请人工确认" if needs_review else "",
        "plan": items,
        "rule_source": "出院随访管理制度 V2.3 第 3 章",
    }
