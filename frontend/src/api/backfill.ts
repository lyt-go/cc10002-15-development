/** 补数批次与扫描账单的前端类型，以及字节/扫描量的展示口径。 */

export interface BackfillTask {
  id: number
  status: string
  任务编号: string
  目标表名: string
  开始日期: string
  结束日期: string
  并行度: number | string
  覆盖策略: string
  完成水位: string
  失败区间: string
  生效时区?: string
  每日扫描字节?: number
  abnormal?: boolean
}

export type SliceStatus = '待扫描' | '成功' | '失败'

export interface BackfillSlice {
  id: number
  task_id: number
  slice_id: string
  checkpoint_seq: number
  start_date: string
  end_date: string
  slice_status: SliceStatus
  scan_bytes: number
  attempt_count: number
  failure_code: string | null
  stat_month: string
  timezone: string
  updated_at: string | null
}

export interface ScanBatch {
  id: number
  batch_id: string
  task_id: number
  任务编号: string
  目标表名: string
  slice_id: string
  checkpoint_seq: number
  attempt_round: number
  round_label: string
  start_date: string
  end_date: string
  stat_month: string
  planned_scan_bytes: number
  actual_scan_bytes: number
  scan_status: '成功' | '失败'
  failure_code: string | null
  created_at: string
}

export interface ScanDetail {
  batch_id: string
  slice_id: string
  checkpoint_seq: number
  attempt_round: number
  round_label: string
  date: string
  planned_scan_bytes: number
  actual_scan_bytes: number
  status: '成功' | '失败'
  failure_code: string | null
}

export interface BackfillBill {
  id: number
  status: string
  abnormal: boolean
  账单编号: string
  数据表名: string
  每日扫描量GB: number | string
  查询次数: number
  统计月份: string
  backfill_task_id: number
  关联任务编号: string
  计划扫描量字节: number
  实际扫描量字节: number
  重试扫描量字节: number
  扫描批次数: number
  扫描明细: ScanDetail[]
}

export interface BackfillAudit {
  id: number
  记录编号: string
  操作表名: string
  变更类型: string
  变更前值: string
  变更后值: string
  操作人: string
  操作时间: string
  变更原因: string
  abnormal: boolean
  slice_id?: string | null
}

/** 字节数转可读单位：账单底层以字节记账，界面按 GiB 展示并保留两位小数。 */
export function formatBytes(value: number | string | null | undefined): string {
  const bytes = Number(value ?? 0)
  if (!Number.isFinite(bytes) || bytes === 0) {
    return '0 B'
  }
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  const scaled = bytes / 1024 ** exponent
  return `${scaled.toFixed(exponent === 0 ? 0 : 2)} ${units[exponent]}`
}

/** 失败切片的状态徽标颜色与文本统一在这里收口。 */
export function sliceStatusClass(status: string): string {
  if (status === '成功') return 'tag tag-success'
  if (status === '失败') return 'tag tag-failed'
  return 'tag tag-pending'
}
