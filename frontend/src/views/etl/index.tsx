import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/etl'
const columns = [
  "任务编号",
  "任务名称",
  "输入源",
  "转换步骤",
  "输出目标",
  "调度周期",
  "依赖关系",
  "任务状态"
]
const actions = [
  "启动任务",
  "暂停任务",
  "归档任务"
]

type Row = Record<string, string | number | null>
interface LinkedSample {
  配置编号: string
  数据源名称: string
  采样算法: string
  样本比例: number
  随机种子: number
  分层字段: string
  输出表名: string
  采样结果: Array<Record<string, string | number | null>>
}
interface InputDetail {
  任务id: number
  任务编号: string
  输入源: string
  关联采样配置: LinkedSample[]
  分层输入: Record<string, number>
}

export default function EtlView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [inputDetail, setInputDetail] = useState<InputDetail | null>(null)

  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('准备任务列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据准备流水线列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '数据准备流水线动作未生效')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据准备流水线操作失败')
    }
  }

  async function openInputDetail(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/inputs/${row.id}`)
      if (!response.ok) {
        throw new Error('流水线输入详情读取失败')
      }
      setInputDetail(await response.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '流水线输入详情读取失败')
    }
  }

  return (
    <section className="page" data-module="etl">
      <header className="page-head">
        <div>
          <h2>数据准备流水线管理</h2>
          <p className="page-desc">维护数据准备的转换步骤、调度配置与依赖管理，输入源关联采样样本输出表，分层输入可追溯。</p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出数据准备流水线清单
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
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openInputDetail(row)}>流水线输入详情</button>
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
              <td className="empty-state" colSpan={columns.length + 1}>暂无数据准备流水线数据</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条数据准备流水线记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>

      {inputDetail && (
        <div className="modal-mask" onMouseDown={(event) => { if (event.target === event.currentTarget) setInputDetail(null) }}>
          <div className="modal" role="dialog" aria-modal="true">
            <div className="modal-head">
              <h3>流水线输入详情 · {inputDetail.任务编号}</h3>
              <button className="modal-close" type="button" onClick={() => setInputDetail(null)}>×</button>
            </div>
            <dl className="detail-grid">
              <div>
                <dt>输入源（样本输出表）</dt>
                <dd>{inputDetail.输入源 || '—'}</dd>
              </div>
              <div>
                <dt>分层输入</dt>
                <dd>
                  {Object.keys(inputDetail.分层输入).length === 0
                    ? '该输入为非分层采样，无分层输入'
                    : Object.entries(inputDetail.分层输入).map(([field, count]) => (
                        <span key={field} className="result-tag">{field}: {count} 行</span>
                      ))}
                </dd>
              </div>
            </dl>
            <h4 className="section-title">关联采样配置（随机种子随快照持久化）</h4>
            {inputDetail.关联采样配置.length === 0 ? (
              <p className="kv-list">输入源暂未关联到采样配置，请先在数据采样管理登记并生成样本输出表。</p>
            ) : (
              inputDetail.关联采样配置.map((sample) => (
                <div key={sample.配置编号} style={{ marginBottom: 10 }}>
                  <dl className="detail-grid">
                    <div><dt>配置编号</dt><dd>{sample.配置编号}</dd></div>
                    <div><dt>数据源名称</dt><dd>{sample.数据源名称}</dd></div>
                    <div><dt>采样算法</dt><dd>{sample.采样算法}</dd></div>
                    <div><dt>样本比例</dt><dd>{String(sample.样本比例)}</dd></div>
                    <div><dt>随机种子</dt><dd>{String(sample.随机种子)}</dd></div>
                    <div><dt>分层字段</dt><dd>{sample.分层字段 || '—'}</dd></div>
                    <div><dt>输出表名</dt><dd>{sample.输出表名}</dd></div>
                    <div>
                      <dt>采样结果</dt>
                      <dd>
                        <span className={`result-tag ${sample.采样结果.length === 0 ? 'muted' : ''}`}>
                          {sample.采样结果.length === 0 ? '尚未生成' : `${sample.采样结果.length} 条（已去重）`}
                        </span>
                      </dd>
                    </div>
                  </dl>
                </div>
              ))
            )}
            <div className="modal-actions">
              <button className="btn primary" type="button" onClick={() => setInputDetail(null)}>关闭</button>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
