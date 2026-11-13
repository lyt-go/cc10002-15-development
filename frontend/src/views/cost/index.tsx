import { Fragment, useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'

import { request } from '@/api/client'
import { formatBytes, type BackfillBill, type ScanDetail } from '@/api/backfill'

const ENDPOINT = '/api/cost'
const baseColumns = ['账单编号', '数据表名', '每日扫描量GB', '查询次数', '存储成本', '计算成本', '统计月份']

type Row = Record<string, string | number | null>
type BillRow = Partial<BackfillBill> & Row

export default function CostView() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const [rows, setRows] = useState<BillRow[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {}
    if (searchParams.get('backfill_task_id')) initial.backfill_task_id = searchParams.get('backfill_task_id') as string
    if (searchParams.get('stat_month')) initial.stat_month = searchParams.get('stat_month') as string
    return initial
  })
  const [expanded, setExpanded] = useState<number | null>(null)
  const filterFields = useMemo(() => ['账单编号', '数据表名', '统计月份'], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) throw new Error('成本账单列表读取失败')
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '存储成本列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  function updateFilter(field: string, value: string) {
    setFilters((prev) => {
      const next = { ...prev }
      if (value) next[field] = value
      else delete next[field]
      return next
    })
  }

  function resetFilters() {
    setFilters({})
    setSearchParams({})
  }

  const stats = useMemo(() => {
    const backfillRows = rows.filter((row) => row.backfill_task_id != null) as BackfillBill[]
    const planned = backfillRows.reduce((sum, row) => sum + Number(row.计划扫描量字节 ?? 0), 0)
    const actual = backfillRows.reduce((sum, row) => sum + Number(row.实际扫描量字节 ?? 0), 0)
    const retry = backfillRows.reduce((sum, row) => sum + Number(row.重试扫描量字节 ?? 0), 0)
    return [
      { label: '成本账单总数', value: rows.length },
      { label: '补数计划扫描量', value: formatBytes(planned) },
      { label: '补数实际扫描量', value: formatBytes(actual) },
      { label: '失败重试额外扫描', value: formatBytes(retry) },
    ]
  }, [rows])

  return (
    <section className="page" data-module="cost">
      <header className="page-head">
        <div>
          <h2>存储成本管理</h2>
          <p className="page-desc">
            追踪各数据表的存储和计算成本；补数区间每次完成或失败都会驱动本页账单，
            按任务×目标表×月份归集，首轮与重试扫描量同时可见、历史明细不覆盖。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('成本账单登记入口尚未接入审批流')}>
            登记成本账单
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出存储成本清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => updateFilter(field, event.target.value)}
            />
          </label>
        ))}
        <label className="filter-item">
          <span>补数任务 id</span>
          <input
            value={filters.backfill_task_id ?? ''}
            placeholder="按补数任务过滤"
            onChange={(event) => updateFilter('backfill_task_id', event.target.value)}
          />
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={resetFilters}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {baseColumns.map((column) => <th key={column}>{column}</th>)}
            <th>来源</th>
            <th>计划/实际扫描量</th>
            <th>重试额外扫描</th>
            <th>处置链路</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const isBackfill = row.backfill_task_id != null
            const details = (row.扫描明细 ?? []) as ScanDetail[]
            const isOpen = expanded === Number(row.id)
            return (
              <Fragment key={String(row.id)}>
                <tr className={row.abnormal ? 'failed-row' : ''}>
                  {baseColumns.map((column) => <td key={column}>{row[column] || '—'}</td>)}
                  <td>
                    {isBackfill ? (
                      <span className="tag tag-info">补数扫描 · {String(row.关联任务编号 ?? '')}</span>
                    ) : (
                      <span className="tag tag-pending">手工账单</span>
                    )}
                  </td>
                  <td>
                    {isBackfill
                      ? `${formatBytes(row.计划扫描量字节)} / ${formatBytes(row.实际扫描量字节)}`
                      : '—'}
                  </td>
                  <td>
                    {isBackfill
                      ? <span className={Number(row.重试扫描量字节) > 0 ? 'error-text' : ''}>{formatBytes(row.重试扫描量字节)}</span>
                      : '—'}
                  </td>
                  <td className="row-actions">
                    {isBackfill && (
                      <>
                        <button className="link" type="button" onClick={() => setExpanded(isOpen ? null : Number(row.id))}>
                          {isOpen ? '收起扫描明细' : `扫描明细（${details.length}轮）`}
                        </button>
                        <button
                          className="link"
                          type="button"
                          onClick={() => navigate(`/backfill`)}
                        >
                          回补数页
                        </button>
                      </>
                    )}
                  </td>
                </tr>
                {isOpen && isBackfill && (
                  <tr>
                    <td colSpan={baseColumns.length + 4}>
                      <table className="sub-table">
                        <thead>
                          <tr>
                            <th>batch_id</th><th>slice_id</th><th>轮次</th><th>日期</th>
                            <th>计划扫描量</th><th>实际扫描量</th><th>结果</th><th>failure_code</th>
                          </tr>
                        </thead>
                        <tbody>
                          {details.map((detail) => (
                            <tr
                              key={detail.batch_id}
                              className={detail.attempt_round > 1 ? 'retry-row' : detail.status === '失败' ? 'failed-row' : ''}
                            >
                              <td>{detail.batch_id}</td>
                              <td>{detail.slice_id}</td>
                              <td>{detail.round_label}</td>
                              <td>{detail.date}</td>
                              <td>{formatBytes(detail.planned_scan_bytes)}</td>
                              <td>{formatBytes(detail.actual_scan_bytes)}</td>
                              <td>
                                <span className={detail.status === '成功' ? 'tag tag-success' : 'tag tag-failed'}>
                                  {detail.status}
                                </span>
                              </td>
                              <td>{detail.failure_code || '—'}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </td>
                  </tr>
                )}
              </Fragment>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={baseColumns.length + 4}>暂无成本账单数据</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条成本账单记录{filters.backfill_task_id ? `（已按补数任务 ${filters.backfill_task_id} 过滤）` : ''}</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
