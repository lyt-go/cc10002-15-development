"""可恢复补数批次：区间切片、断点续做、扫描台账、成本账单与变更审计的编排都收在这里。

设计约定（与 store 的内存实现解耦，换数据库时只改持久化层）：

* 一条补数任务按天切成有序区间，落在 ``backfill_slice`` 表，区间状态只允许
  待扫描 -> 成功 / 失败，成功区间不重复扫描、不重复记账。
* 每次真正发生扫描（首轮或失败重试）都写一条不可变的 ``backfill_scan_batch``
  台账，扫描批次是「补数任务 -> 成本账单」的贯通凭证。
* 成本账单落在 ``cost`` 表，按 补数任务 × 目标表 × 统计月份 归集；每轮扫描的
  扫描量以明细数组追加保留，聚合口径只增不改明细，保证重试额外扫描可见且不覆盖历史。
* 每次区间完成或失败都向 ``auditlog`` 写一条变更审计，记录前后值与变更原因。
* 完成水位只在从首个区间起连续成功的前提下推进，遇到失败区间即停在断点。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.store import store

BACKFILL_MODULE = "backfill"
SLICE_MODULE = "backfill_slice"
SCAN_MODULE = "backfill_scan_batch"
COST_MODULE = "cost"
AUDIT_MODULE = "auditlog"

DEFAULT_TIMEZONE = "Asia/Shanghai"
# 演示用的确定性扫描量：每个目标表每天的计划扫描字节数，保证首轮/重试可复算。
DEFAULT_DAILY_SCAN_BYTES = 10 * 1024 ** 3  # 10 GiB
GB_DIVISOR = 1024 ** 3

# 覆盖策略：覆盖写才可能与已持久化的较晚完整水位冲突；追加写天然不回退水位。
OVERWRITE_POLICIES = {"覆盖写", "全量覆盖", "覆盖"}

SLICE_PENDING = "待扫描"
SLICE_SUCCESS = "成功"
SLICE_FAILED = "失败"

TASK_TERMINAL_DONE = "已完成"
TASK_RUNNING = "执行中"


def _parse_date(value: Any) -> date | None:
    """宽松解析日期；样例数据里可能是占位文本，解析不了就返回 None，不抛异常。"""
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _month_bucket(day: date, timezone_name: str) -> str:
    """跨月切片按目标表生效时区解释：取该时区下的年月作为账单统计月份。"""
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:  # 非法时区兜底到默认时区，不让一条脏配置中断整批补数
        tz = ZoneInfo(DEFAULT_TIMEZONE)
    # 日期本身按目标时区的日历日解释，月份桶直接取该日年月；显式走一遍时区便于跨时区扩展。
    localized = datetime(day.year, day.month, day.day, tzinfo=tz)
    return localized.strftime("%Y%m")


def _next_id(module: str) -> int:
    return max((int(row.get("id", 0)) for row in store.rows(module)), default=0) + 1


def _next_seq(prefix: str, module: str, id_field: str) -> str:
    existing = [str(row.get(id_field, "")) for row in store.rows(module)]
    seq = 1
    while f"{prefix}-{seq:04d}" in existing:
        seq += 1
    return f"{prefix}-{seq:04d}"


class BackfillBatchService:
    """补数批次编排服务，所有状态变更只在本类内落库。"""

    # ------------------------------------------------------------------ 查询
    def list_slices(self, task_id: int | None = None, status: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(SLICE_MODULE)
        if task_id is not None:
            rows = [row for row in rows if int(row.get("task_id", 0)) == task_id]
        if status:
            rows = [row for row in rows if row.get("slice_status") == status]
        return sorted(rows, key=lambda row: (int(row.get("task_id", 0)), int(row.get("checkpoint_seq", 0))))

    def list_scan_batches(self, task_id: int | None = None, slice_id: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(SCAN_MODULE)
        if task_id is not None:
            rows = [row for row in rows if int(row.get("task_id", 0)) == task_id]
        if slice_id:
            rows = [row for row in rows if row.get("slice_id") == slice_id]
        return sorted(rows, key=lambda row: int(row.get("id", 0)))

    def list_audits(self, task_id: int) -> list[dict[str, Any]]:
        """补数任务驱动的变更审计：每个区间完成或失败、启停与拒绝都在这里可回溯。"""
        rows = [
            row for row in store.rows(AUDIT_MODULE)
            if int(row.get("backfill_task_id", -1)) == task_id
        ]
        return sorted(rows, key=lambda row: int(row.get("id", 0)))

    def list_backfill_bills(
        self,
        *,
        task_id: int | None = None,
        target_table: str | None = None,
        stat_month: str | None = None,
    ) -> list[dict[str, Any]]:
        """补数驱动的成本账单：只看 cost 表里带 backfill_task_id 的账单行。"""
        rows = [row for row in store.rows(COST_MODULE) if row.get("backfill_task_id") is not None]
        if task_id is not None:
            rows = [row for row in rows if int(row.get("backfill_task_id", 0)) == task_id]
        if target_table:
            rows = [row for row in rows if target_table in str(row.get("数据表名", ""))]
        if stat_month:
            rows = [row for row in rows if str(row.get("统计月份", "")) == stat_month]
        return sorted(rows, key=lambda row: (str(row.get("数据表名", "")), str(row.get("统计月份", ""))))

    # ------------------------------------------------------------- 公共规则
    def latest_persisted_watermark(self, target_table: str, *, exclude_task_id: int | None = None) -> date | None:
        """目标表上已持久化的最晚完整水位（取其它已完成补数任务的完成水位）。"""
        candidates: list[date] = []
        for row in store.rows(BACKFILL_MODULE):
            if exclude_task_id is not None and int(row.get("id", 0)) == exclude_task_id:
                continue
            if str(row.get("目标表名", "")).strip() != target_table:
                continue
            if row.get("status") != TASK_TERMINAL_DONE:
                continue
            watermark = _parse_date(row.get("完成水位"))
            if watermark is not None:
                candidates.append(watermark)
        return max(candidates, default=None)

    def _check_watermark_conflict(self, task: dict[str, Any]) -> str | None:
        """覆盖策略与已持久化较晚完整水位冲突时，返回拒绝原因；不冲突返回 None。"""
        policy = str(task.get("覆盖策略") or "").strip()
        if policy not in OVERWRITE_POLICIES:
            return None  # 追加写不回退水位，允许执行
        target_table = str(task.get("目标表名") or "").strip()
        latest = self.latest_persisted_watermark(target_table, exclude_task_id=int(task.get("id", 0)))
        end_date = _parse_date(task.get("结束日期")) or _parse_date(task.get("开始日期"))
        if latest is not None and end_date is not None and end_date < latest:
            return (
                f"目标表 {target_table} 已存在更晚的完整完成水位 {latest.isoformat()}，"
                f"本次{policy}范围截止 {end_date.isoformat()} 早于该水位；"
                "为避免回退已持久化水位，拒绝执行，请改用追加写或调整补数范围"
            )
        return None

    def _build_slices(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        """把日期范围按天切成有序区间，checkpoint_seq 从 1 开始。"""
        start = _parse_date(task.get("开始日期"))
        end = _parse_date(task.get("结束日期")) or start
        if start is None:
            raise ValueError("补数任务缺少合法的开始日期，无法切片")
        if end < start:
            raise ValueError("结束日期早于开始日期，无法切片")
        timezone_name = str(task.get("生效时区") or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
        task_code = str(task.get("任务编号") or f"BACK-{task.get('id')}")
        # 先组装区间内容，最后统一分配 id：切片表为惰性创建，逐条调 _next_id 会在空表上撞号。
        base_id = max((int(row.get("id", 0)) for row in store.rows(SLICE_MODULE)), default=0)
        slices: list[dict[str, Any]] = []
        cursor = start
        seq = 1
        while cursor <= end:
            day_text = cursor.isoformat()
            slices.append({
                "id": base_id + seq,
                "task_id": int(task["id"]),
                "任务编号": task_code,
                "目标表名": task.get("目标表名"),
                "slice_id": f"{task_code}-S{seq:03d}",
                "checkpoint_seq": seq,
                "start_date": day_text,
                "end_date": day_text,
                "slice_status": SLICE_PENDING,
                "scan_bytes": 0,
                "attempt_count": 0,
                "failure_code": None,
                "stat_month": _month_bucket(cursor, timezone_name),
                "timezone": timezone_name,
                "updated_at": None,
            })
            seq += 1
            cursor += timedelta(days=1)
        return slices

    # ------------------------------------------------------------- 状态流转
    def start(self, task_id: int, *, fail_dates: list[str] | None = None) -> tuple[dict[str, Any] | None, str]:
        """首次启动补数：幂等（重复启动只生效一次），切片后跑到首个失败区间为止。"""
        task = store.find(BACKFILL_MODULE, task_id)
        if task is None:
            return None, f"补数任务 {task_id} 不存在或已归档"

        existing = self.list_slices(task_id)
        if any(row.get("slice_status") != SLICE_PENDING for row in existing):
            return task, "补数批次已启动过，重复启动不生效；如需断点续做请调用续做接口"
        if existing:
            return task, "补数批次已完成切片且尚未执行，重复启动只生效一次"

        conflict = self._check_watermark_conflict(task)
        if conflict is not None:
            self._write_audit(task, action="启动补数", result="拒绝", reason=conflict)
            return None, conflict

        try:
            slices = self._build_slices(task)
        except ValueError as exc:
            return None, str(exc)
        store.rows(SLICE_MODULE).extend(slices)
        task["生效时区"] = str(task.get("生效时区") or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
        task["status"] = TASK_RUNNING
        task["pending"] = True
        task["abnormal"] = False
        task["失败区间"] = ""
        self._write_audit(task, action="启动补数", result="已切片",
                          reason=f"共生成 {len(slices)} 个有序区间，checkpoint_seq 1..{len(slices)}")
        self._run_pending(task, slices, fail_dates=fail_dates, round_label="首轮")
        return store.find(BACKFILL_MODULE, task_id), "补数批次已启动并按区间执行"

    def resume(self, task_id: int, *, fail_dates: list[str] | None = None) -> tuple[dict[str, Any] | None, str]:
        """断点续做：从第一个失败区间继续，已成功区间不重复扫描或记账。"""
        task = store.find(BACKFILL_MODULE, task_id)
        if task is None:
            return None, f"补数任务 {task_id} 不存在或已归档"
        slices = self.list_slices(task_id)
        if not slices:
            return None, "该任务尚未生成区间切片，请先启动补数"
        if all(row.get("slice_status") == SLICE_SUCCESS for row in slices):
            return task, "全部区间均已成功，无需续做"
        failed = [row for row in slices if row.get("slice_status") == SLICE_FAILED]
        if not failed:
            return task, "当前没有失败区间，存在待扫描区间时请直接启动执行"
        first_failed = min(failed, key=lambda row: int(row.get("checkpoint_seq", 0)))
        self._write_audit(
            task,
            action="断点续做",
            result="续跑",
            reason=f"从失败区间 {first_failed['slice_id']}（{first_failed['start_date']}）继续，已成功区间不重复扫描",
        )
        # 只喂失败点及之后的区间；成功区间物理上跳过，绝不重新扫描。
        candidate = [row for row in slices if int(row.get("checkpoint_seq", 0)) >= int(first_failed["checkpoint_seq"])]
        attempt_round = max((int(row.get("attempt_count", 0)) for row in slices), default=0) + 1
        self._run_pending(task, candidate, fail_dates=fail_dates, round_label=f"第{attempt_round}轮重试")
        return store.find(BACKFILL_MODULE, task_id), "补数批次已从失败区间续做"

    # ------------------------------------------------------------- 内部执行
    def _run_pending(
        self,
        task: dict[str, Any],
        slices: list[dict[str, Any]],
        *,
        fail_dates: list[str] | None,
        round_label: str,
    ) -> None:
        """按 checkpoint_seq 顺序执行；成功区间跳过，遇到失败即中断，水位停在断点前。"""
        fail_set = {day.strip() for day in (fail_dates or []) if day.strip()}
        ordered = sorted(slices, key=lambda row: int(row.get("checkpoint_seq", 0)))
        hit_failure = False
        for slice_row in ordered:
            if hit_failure:
                break  # 有序执行：失败区间之后的区间保持待扫描，等下一次续做
            if slice_row.get("slice_status") == SLICE_SUCCESS:
                continue  # 已成功区间不重复扫描、不重复记账

            previous_status = slice_row.get("slice_status")
            is_fail = slice_row["start_date"] in fail_set
            planned_bytes = self._planned_scan_bytes(task, slice_row)
            # 失败也要计入实际扫描（重试额外扫描必须可见），按计划量的 60% 模拟已发生扫描。
            actual_bytes = planned_bytes if not is_fail else int(planned_bytes * 0.6)
            slice_row["attempt_count"] = int(slice_row.get("attempt_count", 0)) + 1
            slice_row["scan_bytes"] = int(slice_row.get("scan_bytes", 0)) + actual_bytes
            slice_row["updated_at"] = datetime.now().isoformat(timespec="seconds")

            scan_batch = self._write_scan_batch(task, slice_row, round_label, planned_bytes, actual_bytes, is_fail)
            self._write_bill(task, slice_row, scan_batch, planned_bytes, actual_bytes, is_fail)

            if is_fail:
                slice_row["slice_status"] = SLICE_FAILED
                slice_row["failure_code"] = "SCAN_PARTITION_TIMEOUT"
                self._write_audit(
                    task,
                    action="区间扫描",
                    result="失败",
                    reason=(f"区间 {slice_row['slice_id']} 扫描失败，failure_code=SCAN_PARTITION_TIMEOUT，"
                            f"已发生扫描 {actual_bytes} 字节并计入账单，水位不推进"),
                    before=previous_status,
                    after=SLICE_FAILED,
                    slice_id=slice_row["slice_id"],
                )
                hit_failure = True
            else:
                slice_row["slice_status"] = SLICE_SUCCESS
                slice_row["failure_code"] = None
                self._write_audit(
                    task,
                    action="区间扫描",
                    result="成功",
                    reason=f"区间 {slice_row['slice_id']} 扫描成功，扫描 {actual_bytes} 字节",
                    before=previous_status,
                    after=SLICE_SUCCESS,
                    slice_id=slice_row["slice_id"],
                )

        self._recompute_task_state(task)

    def _planned_scan_bytes(self, task: dict[str, Any], slice_row: dict[str, Any]) -> int:
        """计划扫描量：优先取任务上配置的每日扫描字节，否则用确定性默认值。"""
        configured = task.get("每日扫描字节")
        try:
            value = int(configured) if configured not in (None, "") else DEFAULT_DAILY_SCAN_BYTES
            return max(value, 0)
        except (TypeError, ValueError):
            return DEFAULT_DAILY_SCAN_BYTES

    def _recompute_task_state(self, task: dict[str, Any]) -> None:
        """重算任务的失败区间与完成水位；水位只认从首个区间起的连续成功。"""
        slices = self.list_slices(int(task["id"]))
        failed = [row for row in slices if row.get("slice_status") == SLICE_FAILED]
        if failed:
            first_failed = min(failed, key=lambda row: int(row.get("checkpoint_seq", 0)))
            task["失败区间"] = f"{first_failed['start_date']}~{first_failed['end_date']}"
            task["abnormal"] = True
        else:
            task["失败区间"] = ""
            task["abnormal"] = False

        watermark: date | None = None
        for row in sorted(slices, key=lambda item: int(item.get("checkpoint_seq", 0))):
            if row.get("slice_status") != SLICE_SUCCESS:
                break  # 连续区间断裂，水位停在断点前
            watermark = _parse_date(row.get("end_date"))
        if watermark is not None:
            task["完成水位"] = watermark.isoformat()

        if slices and all(row.get("slice_status") == SLICE_SUCCESS for row in slices):
            task["status"] = TASK_TERMINAL_DONE
            task["pending"] = False
            self._write_audit(task, action="完成水位推进", result="任务完成",
                              reason=f"连续区间全部成功，完成水位推进至 {task['完成水位']}")
        else:
            task["status"] = TASK_RUNNING
            task["pending"] = True

    # ------------------------------------------------------------- 台账写入
    def _write_scan_batch(
        self,
        task: dict[str, Any],
        slice_row: dict[str, Any],
        round_label: str,
        planned_bytes: int,
        actual_bytes: int,
        is_fail: bool,
    ) -> dict[str, Any]:
        """扫描批次不可变：每次扫描（含重试）新增一行，绝不更新历史行。"""
        batch = {
            "id": _next_id(SCAN_MODULE),
            "batch_id": _next_seq("SCAN", SCAN_MODULE, "batch_id"),
            "task_id": int(task["id"]),
            "任务编号": task.get("任务编号"),
            "目标表名": task.get("目标表名"),
            "slice_id": slice_row["slice_id"],
            "checkpoint_seq": slice_row["checkpoint_seq"],
            "attempt_round": int(slice_row.get("attempt_count", 1)),
            "round_label": round_label,
            "start_date": slice_row["start_date"],
            "end_date": slice_row["end_date"],
            "stat_month": slice_row["stat_month"],
            "planned_scan_bytes": planned_bytes,
            "actual_scan_bytes": actual_bytes,
            "scan_status": "失败" if is_fail else "成功",
            "failure_code": "SCAN_PARTITION_TIMEOUT" if is_fail else None,
            "billed": True,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        store.rows(SCAN_MODULE).append(batch)
        return batch

    def _write_bill(
        self,
        task: dict[str, Any],
        slice_row: dict[str, Any],
        scan_batch: dict[str, Any],
        planned_bytes: int,
        actual_bytes: int,
        is_fail: bool,
    ) -> dict[str, Any]:
        """按 任务×表×月份 归集账单；扫描批次明细追加，历史明细不覆盖。"""
        rows = store.rows(COST_MODULE)
        bill = next(
            (
                row for row in rows
                if row.get("backfill_task_id") == int(task["id"])
                and str(row.get("统计月份", "")) == slice_row["stat_month"]
            ),
            None,
        )
        detail = {
            "batch_id": scan_batch["batch_id"],
            "slice_id": scan_batch["slice_id"],
            "checkpoint_seq": scan_batch["checkpoint_seq"],
            "attempt_round": scan_batch["attempt_round"],
            "round_label": scan_batch["round_label"],
            "date": slice_row["start_date"],
            "planned_scan_bytes": planned_bytes,
            "actual_scan_bytes": actual_bytes,
            "status": scan_batch["scan_status"],
            "failure_code": scan_batch["failure_code"],
        }
        if bill is None:
            bill_no = self._build_bill_no(task, slice_row["stat_month"])
            bill = {
                "id": _next_id(COST_MODULE),
                "status": "已生成",
                "pending": True,
                "abnormal": is_fail,
                "账单编号": bill_no,
                "数据表名": task.get("目标表名"),
                "存储大小GB": "",
                "每日扫描量GB": round(actual_bytes / GB_DIVISOR, 4),
                "查询次数": 1,
                "存储成本": "",
                "计算成本": "",
                "统计月份": slice_row["stat_month"],
                # 补数贯通字段
                "backfill_task_id": int(task["id"]),
                "关联任务编号": task.get("任务编号"),
                "计划扫描量字节": planned_bytes,
                "实际扫描量字节": actual_bytes,
                # 重试扫描量严格只认第 2 次及以后的尝试；首轮失败在扫描明细与实际/计划差额中可见。
                "重试扫描量字节": actual_bytes if int(scan_batch.get("attempt_round", 1)) > 1 else 0,
                "扫描批次数": 1,
                "扫描明细": [detail],
            }
            rows.append(bill)
        else:
            # 聚合口径累加；明细仅追加，保证首轮与重试扫描量同时可见、历史不被覆盖。
            bill["计划扫描量字节"] = int(bill.get("计划扫描量字节", 0)) + planned_bytes
            bill["实际扫描量字节"] = int(bill.get("实际扫描量字节", 0)) + actual_bytes
            if int(scan_batch.get("attempt_round", 1)) > 1:
                bill["重试扫描量字节"] = int(bill.get("重试扫描量字节", 0)) + actual_bytes
            bill["扫描批次数"] = int(bill.get("扫描批次数", 0)) + 1
            bill["每日扫描量GB"] = round(int(bill["实际扫描量字节"]) / GB_DIVISOR, 4)
            bill["查询次数"] = int(bill.get("查询次数", 0)) + 1
            bill["扫描明细"].append(detail)
            if is_fail:
                bill["abnormal"] = True
        return bill

    def _build_bill_no(self, task: dict[str, Any], stat_month: str) -> str:
        """账单号优先取目标表名（如 ORDER -> COST_ORDER_202608），同表同月多任务时追加任务号防重。"""
        table = str(task.get("目标表名") or "").strip()
        base = f"COST_{table}_{stat_month}"
        existing = {str(row.get("账单编号", "")) for row in store.rows(COST_MODULE)}
        if base not in existing:
            return base
        suffix = str(task.get("任务编号") or task.get("id")).replace("-", "_")
        candidate = f"{base}_{suffix}"
        seq = 2
        while candidate in existing:
            candidate = f"{base}_{suffix}_{seq}"
            seq += 1
        return candidate

    def _write_audit(
        self,
        task: dict[str, Any],
        *,
        action: str,
        result: str,
        reason: str,
        before: str | None = None,
        after: str | None = None,
        slice_id: str | None = None,
    ) -> dict[str, Any]:
        """每个区间完成或失败、以及启停/拒绝都同步一条变更审计。"""
        entry = {
            "id": _next_id(AUDIT_MODULE),
            "status": "已记录",
            "pending": True,
            "abnormal": result in {"失败", "拒绝"},
            "记录编号": _next_seq("AUDI", AUDIT_MODULE, "记录编号"),
            "操作表名": f"backfill:{task.get('任务编号')}",
            "变更类型": f"{action}-{result}",
            "变更前值": before or "",
            "变更后值": after or "",
            "操作人": "补数批次服务",
            "操作时间": datetime.now().isoformat(timespec="seconds"),
            "变更原因": reason,
            "backfill_task_id": int(task.get("id", 0)),
            "slice_id": slice_id,
        }
        store.rows(AUDIT_MODULE).append(entry)
        return entry


backfill_batch_service = BackfillBatchService()
