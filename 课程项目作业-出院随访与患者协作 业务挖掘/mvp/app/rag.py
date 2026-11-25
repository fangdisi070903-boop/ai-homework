"""知识库检索 + LLM Wiki（知识图谱）扩展（REQ-03）。

思路（不依赖外部大模型，保证可离线复现）：
1. 知识库：院内制度、出院指导、FAQ 按二级标题切成知识块，用 BM25 检索；
2. 知识图谱：`kg_triples.csv` 三元组（头实体, 关系, 尾实体, 出处）用于查询扩展与事实补充；
3. 安全：命中"剂量/停药/换药"等红线时直接转人工，不生成任何用药建议；
4. 出处：每个回答必须返回 `sources`，检索置信度不足时返回 need_human=True。

生产替换点：BM25 → 向量检索；KG → 图数据库；答案生成 → LLM（仍需保留出处校验）。
"""
import csv
import os
import re
from collections import Counter
from typing import Dict, List, Tuple

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
KB_DIR = os.path.join(DATA_DIR, "knowledge_base")
KG_FILE = os.path.join(DATA_DIR, "kg_triples.csv")

K1, B = 1.5, 0.75
MIN_SCORE = 4.0

# 合规红线（EV-16）：涉药调整必须转人工
MED_GUARD = ["剂量", "加量", "减量", "停药", "换药", "换一种药", "吃几片", "能不能停", "可以停"]

# 服务范围闸门：问题必须落在出院随访/慢病管理域内，否则不检索直接转人工
DOMAIN_ANCHORS = [
    "心衰", "心力", "COPD", "慢阻肺", "肺", "糖尿病", "血糖", "足", "高血压", "血压",
    "冠心病", "PCI", "术后", "支架", "出院", "随访", "复诊", "复查", "体重", "尿量",
    "水肿", "气促", "吸入", "饮食", "盐", "饮水", "伤口", "换药", "服药", "依从",
    "随访完成", "失联", "接通", "隐私", "MRN", "纳入随访", "病种", "记录",
]

# 病种 → 专属指导文档：问题点名某病种时优先只在该文档内检索，
# 避免"制度文档"里的大段频次说明把病种指导挤到后面
DISEASE_DOCS = [
    (["心衰", "心力衰竭"], "01-心力衰竭出院指导.md"),
    (["COPD", "慢阻肺"], "02-COPD出院指导.md"),
    (["糖尿病足", "糖尿病", "血糖"], "03-糖尿病与糖尿病足出院指导.md"),
]
HUMAN_CONTACT = "心内科门诊 0571-8888-0001 / 随访组 0571-8888-0002"


def tokenize(text: str) -> List[str]:
    """中文用 单字 + 二元组，英文/数字整体保留。"""
    text = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", " ", text.lower())
    out: List[str] = []
    for seg in text.split():
        if re.search(r"[\u4e00-\u9fff]", seg):
            out.extend(list(seg))
            out.extend(seg[i:i + 2] for i in range(len(seg) - 1))
        else:
            out.append(seg)
    return out


class RagService:
    def __init__(self, kb_dir: str = KB_DIR, kg_file: str = KG_FILE):
        self.chunks: List[dict] = []
        self.idf: Dict[str, float] = {}
        self.avgdl = 1.0
        self.kg: List[Tuple[str, str, str, str]] = []
        self._load_kb(kb_dir)
        self._load_kg(kg_file)

    def _load_kb(self, kb_dir: str) -> None:
        docs = []
        for filename in sorted(os.listdir(kb_dir)):
            if not filename.endswith(".md"):
                continue
            with open(os.path.join(kb_dir, filename), encoding="utf-8") as f:
                raw = f.read()
            title = raw.splitlines()[0].lstrip("# ").strip()
            sections = re.split(r"\n##\s+", raw)
            for index, section in enumerate(sections):
                if index == 0:
                    heading, body = title, section
                else:
                    lines = section.split("\n", 1)
                    heading = lines[0].strip()
                    body = lines[1] if len(lines) > 1 else ""
                body = body.strip()
                if len(body) < 30:
                    continue
                tokens = tokenize(heading + " " + heading + " " + body)
                docs.append({
                    "doc": filename,
                    "title": title,
                    "section": heading,
                    "content": body,
                    "tokens": tokens,
                    "tf": Counter(tokens),
                })
        df = Counter()
        for doc in docs:
            df.update(set(doc["tokens"]))
        n = len(docs) or 1
        self.idf = {t: (n - c + 0.5) / (c + 0.5) for t, c in df.items()}
        self.avgdl = sum(len(d["tokens"]) for d in docs) / n or 1.0
        self.chunks = docs

    def _load_kg(self, kg_file: str) -> None:
        if not os.path.exists(kg_file):
            return
        with open(kg_file, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                self.kg.append((row["head"], row["relation"], row["tail"], row["source"]))

    def _bm25(self, query_tokens: List[str], doc: dict) -> float:
        score = 0.0
        dl = len(doc["tokens"])
        for token in query_tokens:
            if token not in doc["tf"]:
                continue
            idf = self.idf.get(token, 0.0)
            tf = doc["tf"][token]
            score += idf * (tf * (K1 + 1)) / (tf + K1 * (1 - B + B * dl / self.avgdl))
        return score

    def _expand(self, question: str) -> Tuple[List[str], List[dict]]:
        """知识图谱扩展：命中头实体时补充尾实体词与事实。"""
        facts = []
        extra_tokens: List[str] = []
        for head, relation, tail, source in self.kg:
            if head in question:
                facts.append({"head": head, "relation": relation, "tail": tail, "source": source})
                extra_tokens.extend(tokenize(tail))
        return extra_tokens, facts

    @staticmethod
    def _disease_doc(question: str):
        """问题点名了病种时返回该病种的专属指导文档，否则返回 None。"""
        for aliases, filename in DISEASE_DOCS:
            if any(alias in question for alias in aliases):
                return filename
        return None

    def answer(self, question: str) -> dict:
        question = (question or "").strip()
        if not question:
            return self._refuse("问题为空，请重新描述")

        # 1) 医疗安全红线：不生成任何用药建议
        for word in MED_GUARD:
            if word in question:
                return self._refuse(
                    "该问题涉及用药调整，按医院规定不能由系统回答，已转人工：" + HUMAN_CONTACT,
                    reason="medical_safety",
                )

        # 2) 服务范围闸门：不属于出院随访域的问题不检索，直接转人工
        if not any(anchor in question for anchor in DOMAIN_ANCHORS):
            return self._refuse(
                "该问题不在出院随访与慢病管理服务范围内，无法回答。已转人工：" + HUMAN_CONTACT,
                reason="out_of_scope",
            )

        # 3) 检索 + 图谱扩展
        q_tokens = tokenize(question)
        extra_tokens, facts = self._expand(question)
        scored = []
        for doc in self.chunks:
            score = self._bm25(q_tokens, doc) + 0.5 * self._bm25(extra_tokens, doc)
            scored.append((score, doc))
        scored.sort(key=lambda x: x[0], reverse=True)

        # 病种过滤：点名病种时先在专属文档里找；专属文档明显不足才回退到全库
        disease_doc = self._disease_doc(question)
        if disease_doc:
            in_doc = [x for x in scored if x[1]["doc"] == disease_doc]
            if in_doc and in_doc[0][0] >= MIN_SCORE:
                scored = in_doc
        top_score, top_doc = scored[0] if scored else (0.0, None)

        if not top_doc or top_score < MIN_SCORE:
            return self._refuse(
                "知识库中没有找到可靠依据，无法确认。已转人工：" + HUMAN_CONTACT,
                reason="no_evidence",
                kg_facts=facts,
            )

        sources = []
        for score, doc in scored[:3]:
            if score <= 0:
                continue
            sources.append({
                "doc": doc["doc"],
                "title": doc["title"],
                "section": doc["section"],
                "score": round(score, 2),
            })

        content = top_doc["content"]
        if len(content) > 420:
            content = content[:420].rsplit("\n", 1)[0] + "…"

        return {
            "answer": content,
            "sources": sources,
            "kg_facts": facts,
            "need_human": False,
            "confidence": round(top_score, 2),
            "disclaimer": "以上内容来自院内资料，不替代医生诊疗意见；症状加重请及时就医。",
        }

    @staticmethod
    def _refuse(message: str, reason: str = "", kg_facts=None) -> dict:
        return {
            "answer": message,
            "sources": [],
            "kg_facts": kg_facts or [],
            "need_human": True,
            "reason": reason,
            "confidence": 0.0,
        }


_service = None


def get_service() -> RagService:
    global _service
    if _service is None:
        _service = RagService()
    return _service
