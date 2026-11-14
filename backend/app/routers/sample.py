"""数据采样管理接口：维护采样配置，覆盖启用采样、暂停采样、关闭采样等动作。

除基础登记/查询外，还提供：
- PUT  /api/sample/{id}            修改配置（写入失败整体回滚到旧快照）
- GET  /api/sample/results         样本输出表清单（同配置同种子去重）
- POST /api/sample/{id}/generate   生成采样结果（同种子幂等、同输出表并发互斥）
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.sample import service

router = APIRouter(prefix="/api/sample", tags=["数据采样管理"])

LIST_FIELDS = ["配置编号", "数据源名称", "采样算法", "样本比例", "随机种子", "分层字段", "输出表名", "配置状态"]
STATUSES = ["草稿", "已启用", "已暂停", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按配置编号检索"),
    status: str | None = Query(default=None, description="草稿、已启用、已暂停、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按配置编号与状态过滤数据采样管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/results", response_model=dict)
def list_results(entry_id: int | None = Query(default=None, description="可选：只看某条配置的结果")) -> dict[str, Any]:
    """读取样本输出表：同一配置同一随机种子只保留一个采样结果，不重复显示。"""
    items = service.list_results(entry_id)
    return {"module": "sample_result", "total": len(items), "items": items}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据采样管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "sample", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条采样配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"采样配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条采样配置，可选采样字段随配置一起落库；缺必填字段时说明原因。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="采样配置已登记", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修改采样配置。

    values.force_fail=true 时模拟样本输出表写入报错，用于验证回滚：
    写入失败后重新 GET 读到的仍是修改前的旧配置（例如旧 seed=41）。
    """
    force_fail = bool(payload.values.pop("force_fail", False))
    entry, message = service.update_entry(entry_id, payload.values, force_fail=force_fail)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/generate", response_model=ActionResult)
def generate_result(entry_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """生成采样结果并写入样本输出表。

    - 同一配置同一随机种子重复生成：只保留首次结果，直接复用（幂等）。
    - 两个任务并发写同一输出表：只允许一个成功，另一个被拦截。
    - 暂停/关闭状态的配置不生成新输出表。
    - values.seed 可覆盖本次随机种子；values.force_fail=true 模拟写入失败并回滚。
    """
    values = payload.values if payload is not None else {}
    force_fail = bool(values.pop("force_fail", False))
    seed = values.get("seed", values.get("随机种子"))
    entry, message = service.generate_result(entry_id, seed=seed, force_fail=force_fail)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条采样配置执行启用采样、暂停采样、关闭采样；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
