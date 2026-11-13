"""历史补数业务规则：状态流转、字段校验与筛选口径都收在这里。

可恢复批次的区间切片、断点续做、账单与审计编排在 ``backfill_batch`` 服务里，
本模块仍负责任务本身的登记、列表筛选与审批/终止等基础状态流转。
"""
from __future__ import annotations

from typing import Any

from app.services.backfill_batch import DEFAULT_TIMEZONE, _parse_date
from app.store import store

MODULE = "backfill"
REQUIRED_FIELDS = ["任务编号", "目标表名", "开始日期"]
OPTIONAL_FIELDS = ["结束日期", "并行度", "覆盖策略", "完成水位", "失败区间", "生效时区", "每日扫描字节"]
STATUS_ORDER = ["草稿", "补数审批中", "执行中", "已完成", "已终止"]
ACTION_RULES = {"提交补数审批": "补数审批中", "启动补数": "执行中", "终止补数": "已终止"}
NEGATIVE_ACTIONS = []


class BackfillService:
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

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        if _parse_date(values.get("开始日期")) is None:
            return None, ["开始日期（需为 YYYY-MM-DD）"]
        end_text = str(values.get("结束日期") or "").strip()
        if end_text and _parse_date(end_text) is None:
            return None, ["结束日期（需为 YYYY-MM-DD）"]
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in OPTIONAL_FIELDS:
            if values.get(field) not in (None, ""):
                entry[field] = values.get(field)
        entry["结束日期"] = end_text or str(values.get("开始日期"))
        entry["并行度"] = values.get("并行度") or 1
        entry["覆盖策略"] = values.get("覆盖策略") or "追加写"
        entry["完成水位"] = values.get("完成水位") or ""
        entry["失败区间"] = values.get("失败区间") or ""
        entry["生效时区"] = str(values.get("生效时区") or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"补数任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于历史补数可执行范围"
        # 「启动补数」统一收口到可恢复批次服务：保证幂等、水位冲突校验与账单/审计联动不被绕过。
        if action == "启动补数":
            from app.services.backfill_batch import backfill_batch_service

            return backfill_batch_service.start(entry_id)
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"补数任务已{action}"
