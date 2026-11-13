"""历史补数接口：维护补数任务，并提供可恢复补数批次的启动、断点续做与处置链路查询。

新增的批次类接口：

* ``POST /{entry_id}/batches/start``  首次启动（幂等，重复启动只生效一次）
* ``POST /{entry_id}/batches/resume`` 从失败区间断点续做
* ``GET  /{entry_id}/slices``         区间切片明细（含 scan_bytes/failure_code/checkpoint_seq）
* ``GET  /{entry_id}/scan-batches``   扫描批次台账（首轮与重试的不可变扫描凭证）
* ``GET  /bills``                     补数驱动的成本账单（任务×目标表×月份）
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.backfill import BackfillService
from app.services.backfill_batch import backfill_batch_service

router = APIRouter(prefix="/api/backfill", tags=["历史补数"])

service = BackfillService()

LIST_FIELDS = ["任务编号", "目标表名", "开始日期", "结束日期", "并行度", "覆盖策略", "完成水位", "失败区间"]
STATUSES = ["草稿", "补数审批中", "执行中", "已完成", "已终止"]


class BatchStartPayload(BaseModel):
    """批次执行参数：fail_dates 用于演示/演练确定性失败，生产执行留空即可。"""

    fail_dates: list[str] = []
    remark: str | None = None


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按任务编号检索"),
    status: str | None = Query(default=None, description="草稿、补数审批中、执行中、已完成、已终止"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按任务编号与状态过滤历史补数列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/bills")
def list_bills(
    task_id: int | None = Query(default=None, description="按补数任务 id 过滤成本账单"),
    target_table: str | None = Query(default=None, description="按目标表名模糊过滤"),
    stat_month: str | None = Query(default=None, description="按统计月份 YYYYMM 过滤"),
) -> dict[str, Any]:
    """补数贯通的成本账单：每轮扫描明细追加保留，可下钻到扫描批次。"""
    items = backfill_batch_service.list_backfill_bills(
        task_id=task_id, target_table=target_table, stat_month=stat_month
    )
    return {"total": len(items), "items": items}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出历史补数清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "backfill", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条补数任务明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"补数任务 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/slices")
def list_slices(entry_id: int, status: str | None = Query(default=None)) -> dict[str, Any]:
    """区间切片：slice_id、start_date、end_date、scan_bytes、failure_code、checkpoint_seq。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"补数任务 {entry_id} 不存在或已归档")
    items = backfill_batch_service.list_slices(task_id=entry_id, status=status)
    return {"task_id": entry_id, "total": len(items), "items": items}


@router.get("/{entry_id}/scan-batches")
def list_scan_batches(entry_id: int) -> dict[str, Any]:
    """扫描批次台账：不可变，记录每轮（含失败重试）的计划/实际扫描量与 failure_code。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"补数任务 {entry_id} 不存在或已归档")
    items = backfill_batch_service.list_scan_batches(task_id=entry_id)
    return {"task_id": entry_id, "total": len(items), "items": items}


@router.get("/{entry_id}/audits")
def list_audits(entry_id: int) -> dict[str, Any]:
    """补数任务的变更审计流：区间成功/失败、水位推进、拒绝原因全部留痕。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"补数任务 {entry_id} 不存在或已归档")
    items = backfill_batch_service.list_audits(entry_id)
    return {"task_id": entry_id, "total": len(items), "items": items}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条补数任务，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段或格式不合法：{'、'.join(missing)}")
    return ActionResult(ok=True, message="补数任务已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条补数任务执行提交补数审批、启动补数、终止补数；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/batches/start", response_model=ActionResult)
def start_batch(entry_id: int, payload: BatchStartPayload | None = None) -> ActionResult:
    """首次启动补数批次：切片后按序执行，重复启动只生效一次，冲突水位会被拒绝并说明原因。"""
    payload = payload or BatchStartPayload()
    entry, message = backfill_batch_service.start(entry_id, fail_dates=payload.fail_dates)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/batches/resume", response_model=ActionResult)
def resume_batch(entry_id: int, payload: BatchStartPayload | None = None) -> ActionResult:
    """断点续做：从第一个失败区间继续，已成功区间不重复扫描或记账。"""
    payload = payload or BatchStartPayload()
    entry, message = backfill_batch_service.resume(entry_id, fail_dates=payload.fail_dates)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
