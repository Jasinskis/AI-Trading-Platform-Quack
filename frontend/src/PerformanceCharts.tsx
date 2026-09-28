import { useEffect, useMemo, useState } from 'react'
import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { format } from 'date-fns'

export type ChartMode = 'return' | 'equity' | 'drawdown' | 'cash' | 'position'

type ChartPoint = {
  ts: string
  equity: number
  cash_used: number
  cash_unused: number
  day_pnl: number
  day_pnl_pct: number
  return_pct: number
  drawdown_pct: number
  positions: Record<
    string,
    {
      market_value: number
      unrealized_pnl: number
      unrealized_pnl_pct: number
      allocation_pct: number
      side?: string
    }
  >
}

type SeriesResponse = {
  capital: number
  symbols: string[]
  points: ChartPoint[]
}

const MODES: { id: ChartMode; label: string }[] = [
  { id: 'return', label: 'Return' },
  { id: 'equity', label: 'Equity' },
  { id: 'drawdown', label: 'Drawdown' },
  { id: 'cash', label: 'Cash used' },
  { id: 'position', label: 'Per position' },
]

function tickLabel(ts: string) {
  try {
    return format(new Date(ts), 'HH:mm')
  } catch {
    return ''
  }
}

export default function PerformanceCharts({
  refreshKey,
  liveSymbols = [],
}: {
  refreshKey?: number
  liveSymbols?: string[]
}) {
  const [mode, setMode] = useState<ChartMode>('equity')
  const [scope, setScope] = useState<'portfolio' | 'symbol'>('portfolio')
  const [symbol, setSymbol] = useState<string>('')
  const [series, setSeries] = useState<SeriesResponse>({ capital: 0, symbols: [], points: [] })
  const [hidden, setHidden] = useState(false)

  async function load() {
    const res = await fetch('/api/charts/series')
    const data = await res.json()
    setSeries(data)
    if (!symbol && data.symbols?.length) setSymbol(data.symbols[0])
  }

  useEffect(() => {
    load().catch(() => undefined)
  }, [refreshKey])

  useEffect(() => {
    const id = setInterval(() => load().catch(() => undefined), 20000)
    return () => clearInterval(id)
  }, [])

  const allSymbols = useMemo(() => {
    const s = new Set([...(series.symbols || []), ...liveSymbols])
    return Array.from(s).sort()
  }, [series.symbols, liveSymbols])

  const chartData = useMemo(() => {
    return (series.points || []).map((p) => {
      const pos = symbol ? p.positions?.[symbol] : undefined
      return {
        t: p.ts,
        label: p.ts ? tickLabel(p.ts) : '',
        equity: p.equity,
        return_pct: p.return_pct,
        drawdown_pct: p.drawdown_pct,
        cash_used: p.cash_used,
        cash_unused: p.cash_unused,
        pos_value: pos?.market_value ?? null,
        pos_pnl: pos?.unrealized_pnl ?? null,
        pos_pnl_pct: pos?.unrealized_pnl_pct ?? null,
        pos_alloc: pos ? pos.allocation_pct * 100 : null,
      }
    })
  }, [series.points, symbol])

  const stroke = 'var(--accent)'
  const strokeNeg = 'var(--short)'

  if (hidden) {
    return (
      <section className="panel chart-panel">
        <header>
          <h2>Performance</h2>
          <button className="ghost" onClick={() => setHidden(false)}>
            Show charts
          </button>
        </header>
      </section>
    )
  }

  const empty = chartData.length < 2

  return (
    <section className="panel chart-panel" style={{ gridColumn: '1 / -1' }}>
      <header>
        <h2>Performance</h2>
        <button className="ghost" onClick={() => setHidden(true)}>
          Hide
        </button>
      </header>
      <div className="body">
        <div className="chart-toolbar">
          <div className="chart-modes">
            {MODES.map((m) => (
              <button
                key={m.id}
                className={mode === m.id ? 'chart-mode active' : 'chart-mode'}
                onClick={() => {
                  setMode(m.id)
                  if (m.id === 'position') setScope('symbol')
                }}
              >
                {m.label}
              </button>
            ))}
          </div>
          <div className="chart-scope">
            <button
              className={scope === 'portfolio' ? 'chart-mode active' : 'chart-mode'}
              onClick={() => setScope('portfolio')}
              disabled={mode === 'position'}
            >
              Portfolio
            </button>
            <button
              className={scope === 'symbol' ? 'chart-mode active' : 'chart-mode'}
              onClick={() => setScope('symbol')}
            >
              Position
            </button>
            {scope === 'symbol' && (
              <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>
                {allSymbols.length === 0 && <option value="">No symbols yet</option>}
                {allSymbols.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            )}
          </div>
        </div>

        <div className="chart-canvas">
          {empty ? (
            <div className="empty">—</div>
          ) : mode === 'cash' && scope === 'portfolio' ? (
            <ResponsiveContainer width="100%" height={260}>
              <AreaChart data={chartData}>
                <CartesianGrid stroke="var(--line)" strokeDasharray="3 3" />
                <XAxis dataKey="label" tick={{ fill: 'var(--muted)', fontSize: 11 }} />
                <YAxis tick={{ fill: 'var(--muted)', fontSize: 11 }} />
                <Tooltip contentStyle={{ background: 'var(--panel-solid)', border: '1px solid var(--line)' }} />
                <Legend />
                <Area type="monotone" dataKey="cash_used" name="Cash used" stroke={stroke} fill={stroke} fillOpacity={0.2} />
                <Area type="monotone" dataKey="cash_unused" name="Cash unused" stroke="var(--muted)" fill="var(--muted)" fillOpacity={0.08} />
              </AreaChart>
            </ResponsiveContainer>
          ) : mode === 'position' || scope === 'symbol' ? (
            <ResponsiveContainer width="100%" height={260}>
              <LineChart data={chartData}>
                <CartesianGrid stroke="var(--line)" strokeDasharray="3 3" />
                <XAxis dataKey="label" tick={{ fill: 'var(--muted)', fontSize: 11 }} />
                <YAxis yAxisId="left" tick={{ fill: 'var(--muted)', fontSize: 11 }} />
                <YAxis yAxisId="right" orientation="right" tick={{ fill: 'var(--muted)', fontSize: 11 }} />
                <Tooltip contentStyle={{ background: 'var(--panel-solid)', border: '1px solid var(--line)' }} />
                <Legend />
                <Line yAxisId="left" type="monotone" dataKey="pos_value" name={`${symbol || 'Pos'} value`} stroke={stroke} dot={false} connectNulls />
                <Line yAxisId="right" type="monotone" dataKey="pos_pnl" name="uPnL $" stroke={strokeNeg} dot={false} connectNulls />
                {mode === 'return' && (
                  <Line yAxisId="right" type="monotone" dataKey="pos_pnl_pct" name="uPnL %" stroke="var(--warn)" dot={false} connectNulls />
                )}
                {mode === 'cash' && (
                  <Line yAxisId="right" type="monotone" dataKey="pos_alloc" name="Alloc %" stroke="var(--warn)" dot={false} connectNulls />
                )}
              </LineChart>
            </ResponsiveContainer>
          ) : (
            <ResponsiveContainer width="100%" height={260}>
              <AreaChart data={chartData}>
                <CartesianGrid stroke="var(--line)" strokeDasharray="3 3" />
                <XAxis dataKey="label" tick={{ fill: 'var(--muted)', fontSize: 11 }} />
                <YAxis tick={{ fill: 'var(--muted)', fontSize: 11 }} />
                <Tooltip contentStyle={{ background: 'var(--panel-solid)', border: '1px solid var(--line)' }} />
                <Legend />
                {mode === 'equity' && (
                  <Area type="monotone" dataKey="equity" name="Equity" stroke={stroke} fill={stroke} fillOpacity={0.18} />
                )}
                {mode === 'return' && (
                  <Area type="monotone" dataKey="return_pct" name="Return %" stroke={stroke} fill={stroke} fillOpacity={0.18} />
                )}
                {mode === 'drawdown' && (
                  <Area type="monotone" dataKey="drawdown_pct" name="Drawdown %" stroke={strokeNeg} fill={strokeNeg} fillOpacity={0.2} />
                )}
              </AreaChart>
            </ResponsiveContainer>
          )}
        </div>
      </div>
    </section>
  )
}
