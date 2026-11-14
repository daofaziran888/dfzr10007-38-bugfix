import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/sample'
const columns = [
  "配置编号",
  "数据源名称",
  "采样算法",
  "样本比例",
  "随机种子",
  "分层字段",
  "输出表名",
  "配置状态"
]
const actions = [
  "启用采样",
  "暂停采样",
  "关闭采样"
]

type Row = Record<string, string | number | null>
type ResultRow = Record<string, string | number | null>

interface FormState {
  配置编号: string
  数据源名称: string
  采样算法: string
  样本比例: string
  随机种子: string
  分层字段: string
  输出表名: string
}

const EMPTY_FORM: FormState = {
  配置编号: '',
  数据源名称: '',
  采样算法: '',
  样本比例: '',
  随机种子: '',
  分层字段: '',
  输出表名: '',
}
const REQUIRED: Array<keyof FormState> = ['配置编号', '数据源名称', '采样算法']
const FIELD_HINTS: Record<keyof FormState, string> = {
  配置编号: '必填，如 SAMP-0004',
  数据源名称: '必填，采样来源表',
  采样算法: '必填，如分层随机采样',
  样本比例: '可选，0.2 或 20% 均可，缺省 10%',
  随机种子: '可选，整数；同种子重复生成结果可复现，缺省 1',
  分层字段: '可选，分层随机采样的分层列',
  输出表名: '可选，留空按配置编号自动生成',
}

export default function SampleView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [successMessage, setSuccessMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [formOpen, setFormOpen] = useState(false)
  const [form, setForm] = useState<FormState>(EMPTY_FORM)
  const [saving, setSaving] = useState(false)
  const [detail, setDetail] = useState<Row | null>(null)
  const [detailResults, setDetailResults] = useState<ResultRow[]>([])

  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('采样配置列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据采样管理列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  function patchForm(field: keyof FormState, value: string) {
    setForm((prev) => ({ ...prev, [field]: value }))
  }

  async function submitForm() {
    setErrorMessage('')
    setSuccessMessage('')
    const missing = REQUIRED.filter((field) => !form[field].trim())
    if (missing.length > 0) {
      setErrorMessage(`缺少必填字段：${missing.join('、')}`)
      return
    }
    setSaving(true)
    try {
      const response = await request(ENDPOINT, {
        method: 'POST',
        body: JSON.stringify({
          // 可选采样字段与必填字段一起提交，后端整包落库，不再静默丢弃
          values: Object.fromEntries(Object.entries(form).filter(([, v]) => v.trim() !== '')),
        }),
      })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '采样配置登记失败')
      }
      setSuccessMessage(`采样配置 ${payload.entry.配置编号} 已登记，随机种子=${payload.entry.随机种子} 已随配置落库`)
      setFormOpen(false)
      setForm(EMPTY_FORM)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '采样配置登记失败')
    } finally {
      setSaving(false)
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setSuccessMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '数据采样管理动作未生效')
      }
      setSuccessMessage(payload.message)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据采样管理操作失败')
    }
  }

  async function generate(row: Row) {
    setErrorMessage('')
    setSuccessMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/generate`, {
        method: 'POST',
        body: JSON.stringify({ values: { seed: row.随机种子 ?? '' } }),
      })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '采样结果生成失败')
      }
      setSuccessMessage(payload.message)
      await reload()
      if (detail && String(detail.id) === String(row.id)) {
        await openDetail(row.id)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '采样结果生成失败')
    }
  }

  async function saveSeed(row: Row) {
    setErrorMessage('')
    setSuccessMessage('')
    const input = window.prompt(`保存 ${row.配置编号} 的随机种子（当前 ${row.随机种子}）：`, String(row.随机种子 ?? ''))
    if (input === null) {
      return
    }
    const forceFail = window.confirm('是否模拟样本输出表写入报错？\n确定=模拟失败并回滚到旧配置；取消=正常保存。')
    try {
      const response = await request(`${ENDPOINT}/${row.id}`, {
        method: 'PUT',
        body: JSON.stringify({ values: { 随机种子: input, force_fail: forceFail } }),
      })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        // 写入报错：后端已按旧快照回滚，这里明确提示回读后的旧种子
        throw new Error(payload.message ?? '保存失败')
      }
      setSuccessMessage(`${payload.message}，随机种子=${payload.entry.随机种子}`)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '保存失败，配置已保持原样')
      await reload()
    }
  }

  async function openDetail(id: string | number | null) {
    setErrorMessage('')
    const detailId = String(id ?? '')
    try {
      const [entryResp, resultResp] = await Promise.all([
        request(`${ENDPOINT}/${detailId}`),
        request(`${ENDPOINT}/results?entry_id=${encodeURIComponent(detailId)}`),
      ])
      if (!entryResp.ok) {
        throw new Error('采样配置明细读取失败')
      }
      const entry = await entryResp.json()
      const resultPayload = resultResp.ok ? await resultResp.json() : { items: [] }
      setDetail(entry)
      setDetailResults(resultPayload.items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '明细读取失败')
    }
  }

  return (
    <section className="page" data-module="sample">
      <header className="page-head">
        <div>
          <h2>数据采样管理管理</h2>
          <p className="page-desc">维护数据采样的算法选择、比例控制与分层配置，为数据采样管理提供可验证的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => { setForm(EMPTY_FORM); setFormOpen(true) }}>
            登记采样配置
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出数据采样管理清单
          </button>
        </div>
      </header>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>采样与动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openDetail(row.id)}>配置/结果详情</button>
                <button className="link" type="button" onClick={() => void generate(row)}>生成采样</button>
                <button className="link" type="button" onClick={() => void saveSeed(row)}>保存种子</button>
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无数据采样管理数据，可先登记采样配置</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条数据采样管理记录</span>
        {successMessage && <span className="success-text">{successMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>

      {formOpen && (
        <div className="modal-mask" onMouseDown={(event) => { if (event.target === event.currentTarget) setFormOpen(false) }}>
          <div className="modal" role="dialog" aria-modal="true">
            <div className="modal-head">
              <h3>登记采样配置</h3>
              <button className="modal-close" type="button" onClick={() => setFormOpen(false)}>×</button>
            </div>
            <div className="form-grid">
              {(Object.keys(EMPTY_FORM) as Array<keyof FormState>).map((field) => (
                <label key={field} className={`form-item${field === '输出表名' ? ' full' : ''}`}>
                  <label htmlFor={`field-${field}`}>
                    {field}
                    {REQUIRED.includes(field) && <span className="required">*</span>}
                  </label>
                  <input
                    id={`field-${field}`}
                    value={form[field]}
                    placeholder={FIELD_HINTS[field]}
                    onChange={(event) => patchForm(field, event.target.value)}
                  />
                  <span className="hint">{FIELD_HINTS[field]}</span>
                </label>
              ))}
            </div>
            <div className="modal-actions">
              <button className="btn ghost" type="button" onClick={() => setFormOpen(false)}>取消</button>
              <button className="btn primary" type="button" disabled={saving} onClick={() => void submitForm()}>
                {saving ? '提交中…' : '提交登记'}
              </button>
            </div>
          </div>
        </div>
      )}

      {detail && (
        <div className="modal-mask" onMouseDown={(event) => { if (event.target === event.currentTarget) setDetail(null) }}>
          <div className="modal" role="dialog" aria-modal="true">
            <div className="modal-head">
              <h3>采样配置与样本输出表 · {String(detail.配置编号)}</h3>
              <button className="modal-close" type="button" onClick={() => setDetail(null)}>×</button>
            </div>
            <dl className="detail-grid">
              {columns.map((column) => (
                <div key={column}>
                  <dt>{column}</dt>
                  <dd>{detail[column] ?? '—'}</dd>
                </div>
              ))}
            </dl>
            <h4 className="section-title">
              样本输出表结果
              <span className={`result-tag ${detailResults.length === 0 ? 'muted' : ''}`}>
                {detailResults.length === 0 ? '尚未生成' : `${detailResults.length} 条（同配置同种子已去重）`}
              </span>
            </h4>
            {detailResults.length === 0 ? (
              <p className="kv-list">暂无采样结果，点击列表中的「生成采样」写入样本输出表；暂停/关闭状态不会生成新输出表。</p>
            ) : (
              <table className="data-table">
                <thead>
                  <tr>
                    <th>随机种子</th>
                    <th>样本比例</th>
                    <th>样本行数</th>
                    <th>分层输入</th>
                    <th>输出表名</th>
                  </tr>
                </thead>
                <tbody>
                  {detailResults.map((result) => (
                    <tr key={String(result.id)}>
                      <td>{String(result.随机种子)}</td>
                      <td>{String(result.样本比例)}</td>
                      <td>{String(result.样本行数)}</td>
                      <td className="kv-list">{JSON.stringify(result.分层输入)}</td>
                      <td>{String(result.输出表名)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <div className="modal-actions">
              <button className="btn" type="button" onClick={() => void generate(detail)}>按当前种子生成/复用</button>
              <button className="btn primary" type="button" onClick={() => setDetail(null)}>关闭</button>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
