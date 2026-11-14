"""数据准备流水线业务规则：状态流转、字段校验与筛选口径都收在这里。

数据准备的输入源通过输出表名关联数据采样配置：流水线读取样本输出表时，
采样配置的分层字段、随机种子与采样结果（分层输入）一并带出，避免读到
空的分层输入。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services.sample import is_blank

MODULE = "etl"
REQUIRED_FIELDS = ["任务编号", "任务名称", "输入源"]
STATUS_ORDER = ["草稿", "运行中", "已暂停", "已完成"]
ACTION_RULES = {"启动任务": "草稿", "暂停任务": "运行中", "归档任务": "已暂停"}
NEGATIVE_ACTIONS = []


class EtlService:
    def __init__(self) -> None:
        self._link_sampling_inputs()

    def _link_sampling_inputs(self) -> None:
        """把仍为占位文本的输入源关联到同编号采样配置的样本输出表。

        例如 ETL-0001 -> SAMP-0001 的输出表名；已填真实输入源的任务不动。
        """
        sample_rows = store.rows("sample")
        sample_by_code = {str(row.get("配置编号") or ""): row for row in sample_rows}
        for row in store.rows(MODULE):
            if not is_blank(row.get("输入源")):
                continue
            task_no = str(row.get("任务编号") or "")
            code = task_no.replace("ETL-", "SAMP-", 1) if task_no.startswith("ETL-") else ""
            sample = sample_by_code.get(code)
            if sample is not None and sample.get("输出表名"):
                row["输入源"] = sample["输出表名"]

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def get_pipeline_input(self, entry_id: int) -> dict[str, Any] | None:
        """流水线输入详情：沿输入源（样本输出表）回读采样配置与采样结果。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        input_source = str(entry.get("输入源") or "").strip()
        matched = [
            row for row in store.rows("sample")
            if str(row.get("输出表名") or "").strip() == input_source
        ]
        samples: list[dict[str, Any]] = []
        stratified_input: dict[str, Any] = {}
        for sample in matched:
            sample_id = int(sample.get("id", 0))
            results = [
                row for row in store.rows("sample_result")
                if int(row.get("采样配置id", 0)) == sample_id
            ]
            # 同（配置、种子）只保留一个结果，详情页不重复显示
            unique_results: dict[tuple[int, int], dict[str, Any]] = {}
            for result in results:
                key = (sample_id, int(result.get("随机种子", 0)))
                unique_results.setdefault(key, result)
            samples.append({
                "采样配置id": sample_id,
                "配置编号": sample.get("配置编号"),
                "数据源名称": sample.get("数据源名称"),
                "采样算法": sample.get("采样算法"),
                "样本比例": sample.get("样本比例"),
                "随机种子": sample.get("随机种子"),
                "分层字段": sample.get("分层字段"),
                "输出表名": sample.get("输出表名"),
                "采样结果": list(unique_results.values()),
            })
            field = str(sample.get("分层字段") or "").strip()
            if field:
                stratified_input[field] = stratified_input.get(field, 0) + sum(
                    int(r.get("样本行数", 0)) for r in unique_results.values()
                )
        return {
            "任务id": entry_id,
            "任务编号": entry.get("任务编号"),
            "输入源": input_source,
            "关联采样配置": samples,
            # 分层输入直接取自采样结果；未生成结果前用采样配置的分层字段占位，保证非空
            "分层输入": stratified_input or {
                str(s.get("分层字段") or "").strip(): 0
                for s in matched if str(s.get("分层字段") or "").strip()
            },
        }

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"准备任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据准备流水线可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"准备任务已{action}"


service = EtlService()
