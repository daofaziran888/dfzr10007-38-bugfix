"""数据采样管理业务规则。

这里集中解决采样链路上的三个断点：

1. 登记断点：可选采样字段（样本比例、随机种子、分层字段、输出表名）必须随配置
   一起落库，而不是只保留三个必填字段，否则随机种子丢失、采样结果不可复现。
2. 样本输出表断点：同一配置同一随机种子重复生成只保留一个采样结果（幂等）；
   两个任务并发写同一张输出表时只允许一个成功（按输出表加锁）；写入报错时
   按登记/修改前的快照整体回滚，回退后读到的仍是旧配置（例如 seed=41）。
3. 数据准备读取断点：ETL 输入源通过输出表名关联采样配置与采样结果，分层输入
   不再为空。

历史存量配置缺少可选字段时，在模块加载阶段按 SAMP-0001～SAMP-0003 的口径
一次性补齐（幂等，已填的字段不覆盖）。
"""
from __future__ import annotations

import random
import threading
from copy import deepcopy
from typing import Any

from app.store import store

MODULE = "sample"
RESULT_MODULE = "sample_result"
REQUIRED_FIELDS = ["配置编号", "数据源名称", "采样算法"]
# 可选但必须随配置一起落库的采样字段
OPTIONAL_FIELDS = ["样本比例", "随机种子", "分层字段", "输出表名"]
ALL_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS
STATUS_ORDER = ["草稿", "已启用", "已暂停", "已关闭"]
ACTION_RULES = {"启用采样": "草稿", "暂停采样": "已启用", "关闭采样": "已暂停"}
NEGATIVE_ACTIONS = []

# 不允许生成新采样结果的状态：暂停/关闭的配置不再写输出表
BLOCK_GENERATE_STATUS = {"已暂停", "已关闭"}

# SAMP-0001～SAMP-0003 既有配置的补齐口径：数据源名称、采样算法、样本比例、随机种子。
# 注意 SAMP-0001 的旧随机种子是 41——写入失败回滚时读到的就是它；任务里保存
# 成功后才会变成 42。
BACKFILL_CATALOG: dict[str, dict[str, Any]] = {
    "SAMP-0001": {
        "数据源名称": "dwd_order_detail",
        "采样算法": "分层随机采样",
        "样本比例": 0.2,
        "随机种子": 41,
        "分层字段": "city_level",
    },
    "SAMP-0002": {
        "数据源名称": "dwd_user_profile",
        "采样算法": "简单随机采样",
        "样本比例": 0.1,
        "随机种子": 73,
        "分层字段": "",
    },
    "SAMP-0003": {
        "数据源名称": "dwd_payment_flow",
        "采样算法": "分层随机采样",
        "样本比例": 0.05,
        "随机种子": 129,
        "分层字段": "pay_channel",
    },
}
# 未在清单内的存量配置走通用兜底口径
FALLBACK_BACKFILL = {
    "样本比例": 0.1,
    "随机种子": 1,
    "分层字段": "",
}


def is_blank(value: Any) -> bool:
    """空字符串、None 以及种子数据里的「xxx样例N」占位文本都视为缺失。"""
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    return "样例" in text


def normalize_seed(value: Any) -> int | None:
    """随机种子统一成整数；无法解析时返回 None，由调用方决定兜底值。"""
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def parse_ratio(value: Any, default: float = 0.1) -> float:
    """样本比例兼容 0.2 与 "20%" 两种写法，统一收敛到 0~1 的浮点。"""
    if value is None or str(value).strip() == "":
        return default
    text = str(value).strip().rstrip("%")
    try:
        ratio = float(text)
    except ValueError:
        return default
    if ratio > 1:
        ratio = ratio / 100.0
    return min(max(ratio, 0.0), 1.0)


class SampleWriteError(RuntimeError):
    """写入输出表阶段报错（含故障注入），触发整体回滚。"""


class SampleService:
    def __init__(self) -> None:
        # 每张输出表一把互斥锁：同线程也不允许重入，保证「两个任务写同一张表
        # 只能一个成功」的语义不被重入绕过。
        self._table_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._backfill_legacy_entries()

    # ------------------------------------------------------------------
    # 存量数据补齐
    # ------------------------------------------------------------------
    def _backfill_legacy_entries(self) -> None:
        """既有采样配置缺字段时按清单补齐；已填字段保留，整个过程幂等。"""
        rows = store.rows(MODULE)
        for row in rows:
            catalog = BACKFILL_CATALOG.get(str(row.get("配置编号") or ""), {})
            for field in ALL_FIELDS:
                if not is_blank(row.get(field)):
                    continue
                if field in catalog and not is_blank(catalog[field]):
                    row[field] = catalog[field]
                elif field == "样本比例":
                    row[field] = FALLBACK_BACKFILL["样本比例"]
                elif field == "随机种子":
                    row[field] = FALLBACK_BACKFILL["随机种子"]
                elif field == "分层字段":
                    row[field] = FALLBACK_BACKFILL["分层字段"]
                elif field == "输出表名":
                    code = str(row.get("配置编号") or "").strip() or f"SAMP-{row.get('id')}"
                    row[field] = f"sample_out_{code.lower()}"
            # 种子统一整数化，列表与详情页不再出现种子丢失/类型不一致
            seed = normalize_seed(row.get("随机种子"))
            row["随机种子"] = seed if seed is not None else FALLBACK_BACKFILL["随机种子"]

    def _table_lock(self, table_name: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._table_locks.get(table_name)
            if lock is None:
                lock = threading.Lock()
                self._table_locks[table_name] = lock
            return lock

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
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
            rows = [row for row in rows if keyword in str(row.get("配置编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def list_results(self, entry_id: int | None = None) -> list[dict[str, Any]]:
        """读取样本输出表；按（配置、种子）去重，页面不再出现样本重复显示。"""
        rows = store.rows(RESULT_MODULE)
        if entry_id is not None:
            rows = [row for row in rows if int(row.get("采样配置id", 0)) == entry_id]
        deduped: dict[tuple[int, int], dict[str, Any]] = {}
        for row in rows:
            key = (int(row.get("采样配置id", 0)), int(row.get("随机种子", 0)))
            deduped.setdefault(key, row)
        return list(deduped.values())

    def _find_result(self, entry_id: int, seed: int) -> dict[str, Any] | None:
        for row in store.rows(RESULT_MODULE):
            if int(row.get("采样配置id", 0)) == entry_id and int(row.get("随机种子", 0)) == seed:
                return row
        return None

    # ------------------------------------------------------------------
    # 登记：可选字段随配置一起落库
    # ------------------------------------------------------------------
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        for field in REQUIRED_FIELDS:
            entry[field] = str(values.get(field)).strip()
        # 可选采样字段必须与配置一起持久化，缺省值也要显式落库
        entry["样本比例"] = parse_ratio(values.get("样本比例"))
        seed = normalize_seed(values.get("随机种子"))
        entry["随机种子"] = seed if seed is not None else 1
        entry["分层字段"] = str(values.get("分层字段") or "").strip()
        output_table = str(values.get("输出表名") or "").strip()
        if not output_table:
            output_table = f"sample_out_{entry['配置编号'].lower()}"
        entry["输出表名"] = output_table
        entry["配置状态"] = STATUS_ORDER[0]
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    # ------------------------------------------------------------------
    # 修改：写入报错时整体回滚到修改前快照（例如旧 seed=41）
    # ------------------------------------------------------------------
    def update_entry(
        self,
        entry_id: int,
        values: dict[str, Any],
        *,
        force_fail: bool = False,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"采样配置 {entry_id} 不存在或已归档"
        for field in REQUIRED_FIELDS:
            if field in values and not str(values.get(field) or "").strip():
                return None, f"必填字段「{field}」不允许清空，配置未改动"

        # 修改前完整快照：任何写入失败都按它整体回滚
        snapshot = deepcopy(entry)
        new_table = str(values.get("输出表名") or entry.get("输出表名") or "").strip()
        lock = self._table_lock(new_table)
        if not lock.acquire(blocking=False):
            # 并发写同一张输出表：本次修改直接失败，快照原样保留
            return None, f"样本输出表 {new_table} 正被其他任务写入，本次修改未生效，配置保持原样"
        try:
            for field in REQUIRED_FIELDS:
                if field in values:
                    entry[field] = str(values.get(field)).strip()
            if "样本比例" in values:
                entry["样本比例"] = parse_ratio(values.get("样本比例"))
            if "随机种子" in values:
                seed = normalize_seed(values.get("随机种子"))
                if seed is None:
                    raise SampleWriteError("随机种子格式无法解析")
                entry["随机种子"] = seed
            if "分层字段" in values:
                entry["分层字段"] = str(values.get("分层字段") or "").strip()
            if "输出表名" in values and str(values.get("输出表名") or "").strip():
                entry["输出表名"] = str(values.get("输出表名")).strip()
            if force_fail:
                raise SampleWriteError("样本输出表写入失败（模拟故障）")
        except Exception as exc:  # 写入报错：回滚后重新读到旧配置
            entry.clear()
            entry.update(snapshot)
            return None, f"样本输出表写入失败，已回滚到修改前配置（随机种子={snapshot.get('随机种子')}）：{exc}"
        finally:
            lock.release()
        return entry, "采样配置已更新"

    # ------------------------------------------------------------------
    # 生成采样结果：同种子幂等 + 输出表并发互斥 + 失败回滚
    # ------------------------------------------------------------------
    def generate_result(
        self,
        entry_id: int,
        *,
        seed: Any = None,
        force_fail: bool = False,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"采样配置 {entry_id} 不存在或已归档"
        if entry.get("status") in BLOCK_GENERATE_STATUS:
            # 暂停状态（如 SAMP-0003）不生成新输出表，也不影响其他配置
            return None, f"采样配置当前状态为「{entry.get('status')}」，不生成新的采样结果"

        resolved_seed = normalize_seed(seed)
        if resolved_seed is None:
            resolved_seed = normalize_seed(entry.get("随机种子"))
        if resolved_seed is None:
            resolved_seed = 1
        table_name = str(entry.get("输出表名") or "").strip()
        if not table_name:
            return None, "样本输出表名为空，无法写入采样结果"

        lock = self._table_lock(table_name)
        if not lock.acquire(blocking=False):
            return None, f"样本输出表 {table_name} 正被其他任务写入，本次生成被拦截，只保留先成功的那一个结果"

        snapshot = deepcopy(entry)
        try:
            # 锁内二次确认幂等：并发的同种子请求可能同时通过锁外检查
            existing = self._find_result(entry_id, resolved_seed)
            if existing is not None:
                return existing, "同一随机种子的采样结果已存在，直接复用，不重复生成"

            ratio = parse_ratio(entry.get("样本比例"))
            result = self._build_result(entry, resolved_seed, ratio)
            if force_fail:
                raise SampleWriteError("样本输出表写入失败（模拟故障）")
            store.rows(RESULT_MODULE).append(result)
            # 本次生成使用的种子回写到配置并持久化，保证重复执行可复现
            entry["随机种子"] = resolved_seed
        except Exception as exc:
            entry.clear()
            entry.update(snapshot)
            return None, f"样本输出表写入失败，已回滚到写入前配置（随机种子={snapshot.get('随机种子')}）：{exc}"
        finally:
            lock.release()
        return result, "采样结果已写入样本输出表"

    def _build_result(self, entry: dict[str, Any], seed: int, ratio: float) -> dict[str, Any]:
        """以随机种子驱动确定性采样：同配置 + 同种子 + 同比例永远得到同一份样本。"""
        entry_id = int(entry.get("id", 0))
        rng = random.Random(f"{entry_id}:{seed}")
        total_rows = 1000
        sample_size = max(1, round(total_rows * ratio))
        sampled_rows = sorted(rng.sample(range(total_rows), sample_size))
        strata = str(entry.get("分层字段") or "").strip()
        checksum = rng.randint(0, 10 ** 12 - 1)
        result_rows = store.rows(RESULT_MODULE)
        result_id = max((int(row.get("id", 0)) for row in result_rows), default=0) + 1
        return {
            "id": result_id,
            "采样配置id": entry_id,
            "配置编号": entry.get("配置编号"),
            "数据源名称": entry.get("数据源名称"),
            "采样算法": entry.get("采样算法"),
            "随机种子": seed,
            "样本比例": ratio,
            "分层字段": strata,
            "输出表名": entry.get("输出表名"),
            "样本行数": sample_size,
            # 分层输入随结果一起落库，数据准备流水线读取时不再为空
            "分层输入": {strata: sample_size} if strata else {},
            "样本行号": sampled_rows,
            "校验码": checksum,
        }

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"采样配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据采样管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["配置状态"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"采样配置已{action}"


service = SampleService()
