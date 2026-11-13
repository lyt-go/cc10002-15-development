import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { request } from '@/api/client'
import {
  formatBytes,
  sliceStatusClass,
  type BackfillAudit,
  type BackfillBill,
  type BackfillSlice,
  type BackfillTask,
  type ScanBatch,
} from '@/api/backfill'

const ENDPOINT = '/api/backfill'
const columns = ['任务编号', '目标表名', '开始日期', '结束日期', '并行度', '覆盖策略', '完成水位', '失败区间']

type Row = Record<string, string | number | null>
type ActionResponse = { ok: boolean; message: string; entry: BackfillTask | null }

const emptyForm = {
  任务编号: '',
  目标表名: '',
  开始日期: '',
  结束日期: '',
  并行度: '1',
  覆盖策略: '追加写',
  生效时区: 'Asia/Shanghai',
  每日扫描字节: '10737418240',
}

export default function BackfillView() {
  const navigate = useNavigate()
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [infoMessage, setInfoMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [showCreate, setShowCreate] = useState(false)
  const [form, setForm] = useState({ ...emptyForm })
  const [detail, setDetail] = useState<BackfillTask | null>(null)
  const [slices, setSlices] = useState<BackfillSlice[]>([])
  const [scanBatches, setScanBatches] = useState<ScanBatch[]>([])
  const [bills, setBills] = useState<BackfillBill[]>([])
  const [audits, setAudits] = useState<BackfillAudit[]>([])
  const [sliceFilter, setSliceFilter] = useState('')
  const [failDatesText, setFailDatesText] = useState('')
  const [busyId, setBusyId] = useState<number | null>(null)
  const filterFields = useMemo(() => ['任务编号', '目标表名', '状态'], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) throw new Error('补数任务列表读取失败')
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '历史补数列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function postAction(path: string, body: unknown): Promise<ActionResponse | null> {
    try {
      const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
      const payload = (await response.json()) as ActionResponse
      if (!payload.ok) setErrorMessage(payload.message)
      else setInfoMessage(payload.message)
      return payload
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '补数操作请求失败')
      return null
    }
  }

  const failDates = useMemo(
    () => failDatesText.split(/[\s,，]+/).map((item) => item.trim()).filter(Boolean),
    [failDatesText],
  )

  async function startBatch(row: Row) {
    setBusyId(Number(row.id))
    const payload = await postAction(`${ENDPOINT}/${row.id}/batches/start`, { fail_dates: failDates })
    setBusyId(null)
    if (payload?.ok) await reload()
  }

  async function resumeBatch(row: Row) {
    setBusyId(Number(row.id))
    const payload = await postAction(`${ENDPOINT}/${row.id}/batches/resume`, { fail_dates: [] })
    setBusyId(null)
    if (payload?.ok) {
      await reload()
      if (detail && Number(detail.id) === Number(row.id)) await openDetail(detail)
    }
  }

  async function submitCreate() {
    const values: Record<string, string | number> = { ...form }
    values.并行度 = Number(form.并行度) || 1
    values.每日扫描字节 = Number(form.每日扫描字节) || 0
    const payload = await postAction(ENDPOINT, { values })
    if (payload?.ok) {
      setShowCreate(false)
      setForm({ ...emptyForm })
      await reload()
    }
  }

  async function openDetail(task: BackfillTask | Row) {
    const id = Number(task.id)
    setDetail(task as BackfillTask)
    setSliceFilter('')
    setErrorMessage('')
    try {
      const [sliceRes, scanRes, billRes, auditRes] = await Promise.all([
        request(`${ENDPOINT}/${id}/slices`),
        request(`${ENDPOINT}/${id}/scan-batches`),
        request(`${ENDPOINT}/bills?task_id=${id}`),
        request(`${ENDPOINT}/${id}/audits`),
      ])
      const slicePayload = await sliceRes.json()
      const scanPayload = await scanRes.json()
      const billPayload = await billRes.json()
      const auditPayload = await auditRes.json()
      setSlices(slicePayload.items ?? [])
      setScanBatches(scanPayload.items ?? [])
      setBills(billPayload.items ?? [])
      setAudits(auditPayload.items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '补数详情读取失败')
    }
  }

  const stats = useMemo(() => {
    const totalCount = rows.length
    const running = rows.filter((row) => row.status === '执行中').length
    const done = rows.filter((row) => row.status === '已完成').length
    const abnormal = rows.filter((row) => Boolean(row.abnormal) || Boolean(row.失败区间)).length
    return [
      { label: '补数任务总数', value: totalCount },
      { label: '执行中任务', value: running },
      { label: '已完成任务', value: done },
      { label: '存在失败区间', value: abnormal },
    ]
  }, [rows])

  const visibleSlices = useMemo(
    () => (sliceFilter ? slices.filter((slice) => slice.slice_status === sliceFilter) : slices),
    [slices, sliceFilter],
  )

  return (
    <section className="page" data-module="backfill">
      <header className="page-head">
        <div>
          <h2>历史补数管理</h2>
          <p className="page-desc">
            可恢复补数批次：日期范围按天切成有序区间，中断后从失败区间续做，完成水位仅在连续区间全部成功后推进；
            每轮扫描量贯通成本账单与变更审计。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => { setForm({ ...emptyForm }); setShowCreate(true) }}>
            登记补数任务
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出历史补数清单
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
            {field === '状态' ? (
              <select value={filters[field] ?? ''} onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}>
                <option value="">全部状态</option>
                {['草稿', '补数审批中', '执行中', '已完成', '已终止'].map((status) => (
                  <option key={status} value={status}>{status}</option>
                ))}
              </select>
            ) : (
              <input
                value={filters[field] ?? ''}
                placeholder={`按${field}检索`}
                onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
              />
            )}
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <div className="inline-filter">
        <span>演练失败日期（可选，YYYY-MM-DD，多个用逗号分隔，留空则全部成功）：</span>
        <input
          value={failDatesText}
          placeholder="例如 2026-08-12"
          onChange={(event) => setFailDatesText(event.target.value)}
          style={{ padding: '4px 8px', border: '1px solid var(--border)', borderRadius: 6, minWidth: 200 }}
        />
      </div>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>状态</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)} className={row.失败区间 ? 'failed-row' : ''}>
              {columns.map((column) => <td key={column}>{row[column] || '—'}</td>)}
              <td><span className="tag tag-info">{String(row.status ?? '')}</span></td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openDetail(row)}>批次详情</button>
                <button
                  className="link"
                  type="button"
                  disabled={busyId === Number(row.id)}
                  onClick={() => void startBatch(row)}
                >
                  启动补数
                </button>
                <button
                  className="link"
                  type="button"
                  disabled={busyId === Number(row.id) || !row.失败区间}
                  title={row.失败区间 ? '从第一个失败区间续做' : '没有失败区间，无需续做'}
                  onClick={() => void resumeBatch(row)}
                >
                  断点续做
                </button>
                <button
                  className="link"
                  type="button"
                  onClick={() => navigate(`/cost?backfill_task_id=${String(row.id)}`)}
                >
                  查看成本账单
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无历史补数数据，可先登记补数任务</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条历史补数记录</span>
        {infoMessage && <span className="success-text">{infoMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>

      {showCreate && (
        <div className="modal-mask" onClick={() => setShowCreate(false)}>
          <div className="modal-card" onClick={(event) => event.stopPropagation()}>
            <div className="modal-head">
              <h3>登记补数任务</h3>
              <button className="modal-close" type="button" onClick={() => setShowCreate(false)}>×</button>
            </div>
            <div className="modal-body">
              <div className="form-grid">
                <label>任务编号 *
                  <input value={form.任务编号} onChange={(e) => setForm({ ...form, 任务编号: e.target.value })} placeholder="例如 BACK_ORDER" />
                </label>
                <label>目标表名 *
                  <input value={form.目标表名} onChange={(e) => setForm({ ...form, 目标表名: e.target.value })} placeholder="例如 ORDER" />
                </label>
                <label>开始日期 *
                  <input type="date" value={form.开始日期} onChange={(e) => setForm({ ...form, 开始日期: e.target.value })} />
                </label>
                <label>结束日期
                  <input type="date" value={form.结束日期} onChange={(e) => setForm({ ...form, 结束日期: e.target.value })} />
                </label>
                <label>并行度
                  <input type="number" min={1} value={form.并行度} onChange={(e) => setForm({ ...form, 并行度: e.target.value })} />
                </label>
                <label>覆盖策略
                  <select value={form.覆盖策略} onChange={(e) => setForm({ ...form, 覆盖策略: e.target.value })}>
                    <option value="追加写">追加写</option>
                    <option value="覆盖写">覆盖写</option>
                  </select>
                </label>
                <label>生效时区（跨月切片按此时区解释）
                  <input value={form.生效时区} onChange={(e) => setForm({ ...form, 生效时区: e.target.value })} />
                </label>
                <label>每日计划扫描字节
                  <input type="number" min={0} value={form.每日扫描字节} onChange={(e) => setForm({ ...form, 每日扫描字节: e.target.value })} />
                </label>
              </div>
              <p className="form-tip">覆盖写范围若早于目标表已持久化的完整完成水位，启动时会被拒绝并给出原因；追加写不回退水位。</p>
            </div>
            <div className="modal-foot">
              <button className="btn ghost" type="button" onClick={() => setShowCreate(false)}>取消</button>
              <button className="btn primary" type="button" onClick={() => void submitCreate()}>提交登记</button>
            </div>
          </div>
        </div>
      )}

      {detail && (
        <div className="modal-mask" onClick={() => setDetail(null)}>
          <div className="modal-card wide" onClick={(event) => event.stopPropagation()}>
            <div className="modal-head">
              <h3>补数批次详情 · {detail.任务编号}（{detail.目标表名}）</h3>
              <button className="modal-close" type="button" onClick={() => setDetail(null)}>×</button>
            </div>
            <div className="modal-body">
              <div className="detail-panel">
                <h4>任务水位与状态</h4>
                <div className="kv-grid">
                  <div className="kv"><span>状态</span><strong>{detail.status}</strong></div>
                  <div className="kv"><span>日期范围</span><strong>{detail.开始日期} ~ {detail.结束日期}</strong></div>
                  <div className="kv"><span>完成水位</span><strong>{detail.完成水位 || '—'}</strong></div>
                  <div className="kv"><span>失败区间</span><strong style={{ color: detail.失败区间 ? 'var(--danger)' : undefined }}>{detail.失败区间 || '无'}</strong></div>
                  <div className="kv"><span>覆盖策略 / 并行度</span><strong>{detail.覆盖策略} / {String(detail.并行度)}</strong></div>
                  <div className="kv"><span>生效时区</span><strong>{detail.生效时区 || 'Asia/Shanghai'}</strong></div>
                </div>
              </div>

              <h4 className="section-title">区间切片（{slices.length} 个，checkpoint_seq 有序，成功区间不重复扫描）</h4>
              <div className="inline-filter">
                <span>状态筛选</span>
                <select value={sliceFilter} onChange={(e) => setSliceFilter(e.target.value)}>
                  <option value="">全部</option>
                  <option value="待扫描">待扫描</option>
                  <option value="成功">成功</option>
                  <option value="失败">失败</option>
                </select>
              </div>
              <table className="sub-table">
                <thead>
                  <tr>
                    <th>slice_id</th><th>checkpoint_seq</th><th>start_date</th><th>end_date</th>
                    <th>状态</th><th>scan_bytes</th><th>尝试次数</th><th>failure_code</th><th>统计月份</th>
                  </tr>
                </thead>
                <tbody>
                  {visibleSlices.map((slice) => (
                    <tr key={slice.slice_id} className={slice.slice_status === '失败' ? 'failed-row' : ''}>
                      <td>{slice.slice_id}</td>
                      <td>{slice.checkpoint_seq}</td>
                      <td>{slice.start_date}</td>
                      <td>{slice.end_date}</td>
                      <td><span className={sliceStatusClass(slice.slice_status)}>{slice.slice_status}</span></td>
                      <td>{formatBytes(slice.scan_bytes)}</td>
                      <td>{slice.attempt_count}</td>
                      <td>{slice.failure_code || '—'}</td>
                      <td>{slice.stat_month}</td>
                    </tr>
                  ))}
                  {visibleSlices.length === 0 && (
                    <tr><td className="empty-state" colSpan={9}>该任务尚未启动，暂无区间切片</td></tr>
                  )}
                </tbody>
              </table>

              <h4 className="section-title">扫描批次台账（{scanBatches.length} 条，不可变；首轮与重试扫描均可追溯）</h4>
              <table className="sub-table">
                <thead>
                  <tr>
                    <th>batch_id</th><th>区间</th><th>轮次</th><th>日期</th><th>计划扫描量</th>
                    <th>实际扫描量</th><th>结果</th><th>failure_code</th>
                  </tr>
                </thead>
                <tbody>
                  {scanBatches.map((batch) => (
                    <tr key={batch.batch_id} className={batch.attempt_round > 1 ? 'retry-row' : batch.scan_status === '失败' ? 'failed-row' : ''}>
                      <td>{batch.batch_id}</td>
                      <td>{batch.slice_id}</td>
                      <td>{batch.round_label}（第 {batch.attempt_round} 次）</td>
                      <td>{batch.start_date}</td>
                      <td>{formatBytes(batch.planned_scan_bytes)}</td>
                      <td>{formatBytes(batch.actual_scan_bytes)}</td>
                      <td><span className={batch.scan_status === '成功' ? 'tag tag-success' : 'tag tag-failed'}>{batch.scan_status}</span></td>
                      <td>{batch.failure_code || '—'}</td>
                    </tr>
                  ))}
                  {scanBatches.length === 0 && (
                    <tr><td className="empty-state" colSpan={8}>暂无扫描批次</td></tr>
                  )}
                </tbody>
              </table>

              <h4 className="section-title">贯通成本账单（任务 × 目标表 × 统计月份，重试扫描只追加不覆盖）</h4>
              <table className="sub-table">
                <thead>
                  <tr>
                    <th>账单编号</th><th>统计月份</th><th>计划扫描量</th><th>实际扫描量</th>
                    <th>重试额外扫描</th><th>扫描批次数</th><th>处置</th>
                  </tr>
                </thead>
                <tbody>
                  {bills.map((bill) => (
                    <tr key={bill.id} className={bill.abnormal ? 'failed-row' : ''}>
                      <td>{bill.账单编号}</td>
                      <td>{bill.统计月份}</td>
                      <td>{formatBytes(bill.计划扫描量字节)}</td>
                      <td>{formatBytes(bill.实际扫描量字节)}</td>
                      <td>{formatBytes(bill.重试扫描量字节)}</td>
                      <td>{bill.扫描批次数}</td>
                      <td>
                        <button className="link" type="button" onClick={() => navigate(`/cost?backfill_task_id=${detail.id}`)}>
                          去成本页核对
                        </button>
                      </td>
                    </tr>
                  ))}
                  {bills.length === 0 && (
                    <tr><td className="empty-state" colSpan={7}>暂无账单，区间执行后自动生成</td></tr>
                  )}
                </tbody>
              </table>

              <h4 className="section-title">变更审计流（{audits.length} 条，每次区间完成或失败同步留痕）</h4>
              <table className="sub-table">
                <thead>
                  <tr>
                    <th>记录编号</th><th>变更类型</th><th>关联区间</th><th>前值</th><th>后值</th>
                    <th>操作时间</th><th>变更原因</th>
                  </tr>
                </thead>
                <tbody>
                  {audits.map((audit) => (
                    <tr key={audit.id} className={audit.abnormal ? 'failed-row' : ''}>
                      <td>{audit.记录编号}</td>
                      <td>
                        <span className={audit.abnormal ? 'tag tag-failed' : 'tag tag-info'}>{audit.变更类型}</span>
                      </td>
                      <td>{audit.slice_id || '—'}</td>
                      <td>{audit.变更前值 || '—'}</td>
                      <td>{audit.变更后值 || '—'}</td>
                      <td>{audit.操作时间}</td>
                      <td>{audit.变更原因}</td>
                    </tr>
                  ))}
                  {audits.length === 0 && (
                    <tr><td className="empty-state" colSpan={7}>暂无审计记录</td></tr>
                  )}
                </tbody>
              </table>
            </div>
            <div className="modal-foot">
              <button className="btn ghost" type="button" onClick={() => void openDetail(detail)}>刷新详情</button>
              <button className="btn primary" type="button" onClick={() => setDetail(null)}>关闭</button>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
