from __future__ import annotations

from .models import Clause, SectionFlags


class ClauseAnalyzer:
    ALGORITHM_KEYWORDS = ("algorithm", "算法", "cluster", "聚类", "score", "打分", "ranking", "排序", "recommend")
    DATA_KEYWORDS = ("table", "schema", "field", "column", "数据库", "表", "字段", "data structure", "model")
    API_KEYWORDS = ("api", "endpoint", "request", "response", "接口", "请求", "响应", "http")
    UI_KEYWORDS = ("ui", "page", "card", "button", "页面", "卡片", "按钮", "展示")

    def detect_sections(self, clause: Clause) -> SectionFlags:
        probe = f"{clause.title}\n{clause.content}".lower()

        has_algorithm = any(token in probe for token in self.ALGORITHM_KEYWORDS)
        has_data_structure = any(token in probe for token in self.DATA_KEYWORDS)
        has_api_contract = any(token in probe for token in self.API_KEYWORDS)
        has_ui = any(token in probe for token in self.UI_KEYWORDS)

        hits = sum([has_algorithm, has_data_structure, has_api_contract, has_ui])
        confidence = 0.55 + (0.1 * hits)
        confidence = min(confidence, 0.95)
        if hits == 0:
            confidence = 0.5

        return SectionFlags(
            has_algorithm=has_algorithm,
            has_data_structure=has_data_structure,
            has_api_contract=has_api_contract,
            has_ui=has_ui,
            confidence=confidence,
        )
