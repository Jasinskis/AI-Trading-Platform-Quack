import PerformanceCharts from './PerformanceCharts'
import FloorFeed from './FloorFeed'
import { useCallback, useEffect, useRef, useState } from 'react'
import { format } from 'date-fns'
import { BookOpen, Moon, Pause, Play, PlayCircle, Square, Sun } from 'lucide-react'

type Position = {
  symbol: string
  side: 'long' | 'short'
  qty: number
  avg_price: number
  last_price: number
  market_value: number
  unrealized_pnl: number
  unrealized_pnl_pct: number
  allocation_pct: number
  source: string
  reason?: string
}

type Portfolio = {
  cash_used: number
  cash_unused: number
  total_equity: number
  day_pnl: number
  day_pnl_pct: number
  positions: Position[]
}

type Message = {
  id?: number
  ts: string
  from_agent: string
  to_agent?: string | null
  content: string
  kind?: string
}

type Trade = {
  id: number
  symbol: string
  side: string
  qty: number
  price: number
  allocation_pct: number
  reason: string
  status: string
  source: string
  ts: string
  broker_ref?: string
}

type LibraryDay = {
  id: number
  trade_date: string
  total_pnl: number
  total_pnl_pct: number
  best_symbol?: string
  worst_symbol?: string
  pros: string[]
  cons: string[]
  mistakes: string[]
  conclusions: string
  agent_scores: Record<string, unknown>
  full_report: Record<string, unknown>
  simulated_after_hours?: boolean
  execution_note?: string
  execution_venue?: string
}

const emptyPortfolio: Portfolio = {
  cash_used: 0,
  cash_unused: 1000,
  total_equity: 1000,
  day_pnl: 0,
  day_pnl_pct: 0,
  positions: [],
}

type Budget = {
  spent_usd: number
  budget_usd: number
  remaining_usd: number
  pct_used: number
  paused: boolean
  pause_reason: string
  input_tokens: number
  output_tokens: number
  calls: number
}

const emptyBudget: Budget = {
  spent_usd: 0,
  budget_usd: 10,
  remaining_usd: 10,
  pct_used: 0,
  paused: false,
  pause_reason: '',
  input_tokens: 0,
  output_tokens: 0,
  calls: 0,
}

const DEFAULT_PROMPT = `Market is already open. Start the desk right now and trade until today's US equity close.

Use about $1000 of capital on top-10 S&P names only. Long or short. Max 30% in any one name, at most 10 names. Prefer high-conviction setups from sentiment + technicals; use fundamentals mainly to avoid earnings landmines.

Flatten everything at the close and write the full day report to the library.`

export default function App() {
  const [prompt, setPrompt] = useState(DEFAULT_PROMPT)
  const [status, setStatus] = useState('idle')
  const [phase, setPhase] = useState('idle')
  const [portfolio, setPortfolio] = useState<Portfolio>(emptyPortfolio)
  const [messages, setMessages] = useState<Message[]>([])
  const [trades, setTrades] = useState<Trade[]>([])
  const [budget, setBudget] = useState<Budget>(emptyBudget)
  const [budgetAlert, setBudgetAlert] = useState<string | null>(null)
  const [selectedTrade, setSelectedTrade] = useState<Trade | null>(null)
  const [selectedMessage, setSelectedMessage] = useState<Message | null>(null)
  const [symbolDetail, setSymbolDetail] = useState<Record<string, unknown> | null>(null)
  const [sapraThread, setSapraThread] = useState<{ role: 'you' | 'sapra'; text: string }[]>([])
  const [sapraInput, setSapraInput] = useState('')
  const [sapraBusy, setSapraBusy] = useState(false)
  const [libraryOpen, setLibraryOpen] = useState(false)
  const [library, setLibrary] = useState<LibraryDay[]>([])
  const [libraryDay, setLibraryDay] = useState<LibraryDay | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [paused, setPaused] = useState(false)
  const [chartTick, setChartTick] = useState(0)
  const [floor, setFloor] = useState<{
    last: { agent: string; task: string } | null
    current: { agent: string; task: string }[]
    next: { agent: string; task: string }[]
    meeting: { members: string[]; topic: string } | null
  }>({ last: null, current: [], next: [], meeting: null })
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    const saved = localStorage.getItem('quack-theme')
    return saved === 'dark' ? 'dark' : 'light'
  })
  const logEnd = useRef<HTMLDivElement>(null)
  const sapraEnd = useRef<HTMLDivElement>(null)

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    localStorage.setItem('quack-theme', theme)
  }, [theme])

  const refreshStatic = useCallback(async () => {
    const fetchJson = async (url: string, ms = 5000) => {
      const ctrl = new AbortController()
      const timer = window.setTimeout(() => ctrl.abort(), ms)
      try {
        const r = await fetch(url, { signal: ctrl.signal })
        if (!r.ok) throw new Error(`${url} ${r.status}`)
        return await r.json()
      } finally {
        window.clearTimeout(timer)
      }
    }

    const settled = await Promise.allSettled([
      fetchJson('/api/portfolio'),
      fetchJson('/api/messages'),
      fetchJson('/api/trades'),
      fetchJson('/api/health', 4000),
      fetchJson('/api/budget', 4000),
      fetchJson('/api/floor', 4000),
    ])

    const val = <T,>(i: number): T | null =>
      settled[i].status === 'fulfilled' ? ((settled[i] as PromiseFulfilledResult<T>).value as T) : null

    const p = val<Portfolio>(0)
    const m = val<{ messages?: Message[] }>(1)
    const t = val<{ trades?: Trade[] }>(2)
    const h = val<{
      status?: string
      phase?: string
      paused?: boolean
      floor?: typeof floor
      session_id?: number | null
    }>(3)
    const b = val<Budget>(4)
    const f = val<typeof floor>(5)

    if (p) setPortfolio(p)
    // Full replace on refresh so a browser reload reconnects to the same live session history
    if (m) setMessages(m.messages || [])
    if (t) setTrades(t.trades || [])
    if (h) {
      setStatus(h.status || 'idle')
      setPhase(h.phase || 'idle')
      setPaused(Boolean(h.paused) || h.status === 'paused' || h.phase === 'paused')
      if (h.floor) setFloor(h.floor)
    }
    if (b) {
      setBudget(b)
      if (b.paused) setBudgetAlert(b.pause_reason || 'Anthropic budget limit reached. Operations paused.')
    }
    if (f) setFloor(f)

    const failed = settled.filter((s) => s.status === 'rejected').length
    if (failed === settled.length) {
      // Keep UI usable; auto-retry handles transient hangs
      setError('Reconnecting to desk…')
    } else {
      setError(null)
    }
  }, [])

  useEffect(() => {
    refreshStatic().catch((e) => setError(String(e)))
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    let ws: WebSocket | null = null
    let closed = false
    let retry: number | undefined

    const connect = () => {
      if (closed) return
      ws = new WebSocket(`${proto}://${location.host}/ws`)
      ws.onopen = () => {
        // Re-sync full state after WS connects (browser reload / reconnect)
        refreshStatic().catch(() => undefined)
      }
      ws.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data)
          if (data.type === 'agent_message') {
            setMessages((prev) => {
              if (data.id && prev.some((x) => x.id === data.id)) return prev
              return [...prev, data]
            })
          } else if (data.type === 'portfolio') {
            setPortfolio(data.data)
            setChartTick((n) => n + 1)
          } else if (data.type === 'snapshot') {
            setChartTick((n) => n + 1)
          } else if (data.type === 'phase') {
            setPhase(data.phase)
            setStatus(data.status)
            setPaused(data.status === 'paused' || data.phase === 'paused')
          } else if (data.type === 'paused') {
            setPaused(Boolean(data.paused))
            if (data.paused) {
              setStatus('paused')
              setPhase('paused')
            }
          } else if (data.type === 'hello') {
            setPhase(data.phase)
            setStatus(data.status)
            if (data.floor) setFloor(data.floor)
            refreshStatic().catch(() => undefined)
          } else if (data.type === 'floor') {
            setFloor(data.data)
          } else if (data.type === 'error') {
            setError(data.message || 'Session error')
            setStatus('error')
          } else if (data.type === 'budget') {
            setBudget(data.data)
            if (data.data?.paused) setBudgetAlert(data.data.pause_reason || 'Budget paused')
          } else if (data.type === 'budget_exceeded') {
            setBudget(data.data)
            setBudgetAlert(data.message || 'Anthropic budget limit reached. Operations paused.')
            setStatus('stopped')
            setPhase('budget_paused')
          } else if (data.type === 'library_update') {
            setLibrary((prev) => [data.data, ...prev.filter((d) => d.trade_date !== data.data.trade_date)])
          }
        } catch {
          /* ignore */
        }
      }
      ws.onclose = () => {
        if (closed) return
        retry = window.setTimeout(connect, 2000)
      }
    }
    connect()

    const ping = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send('ping')
    }, 25000)
    const poll = setInterval(() => {
      refreshStatic().catch(() => undefined)
    }, 20000)
    return () => {
      closed = true
      clearInterval(ping)
      clearInterval(poll)
      if (retry) window.clearTimeout(retry)
      ws?.close()
    }
  }, [refreshStatic])

  async function openSymbol(symbol: string) {
    const res = await fetch(`/api/symbol/${encodeURIComponent(symbol)}`)
    setSymbolDetail(await res.json())
  }

  async function askSapra() {
    const q = sapraInput.trim()
    if (!q || sapraBusy) return
    setSapraBusy(true)
    setSapraInput('')
    setSapraThread((prev) => [...prev, { role: 'you', text: q }])
    try {
      const res = await fetch('/api/floor/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: q }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Floor unavailable')
      setSapraThread((prev) => [...prev, { role: 'sapra', text: data.answer }])
    } catch (e) {
      setSapraThread((prev) => [...prev, { role: 'sapra', text: `Error: ${String(e)}` }])
    } finally {
      setSapraBusy(false)
    }
  }

  async function start() {
    setBusy(true)
    setError(null)
    setStatus('running')
    setPhase('starting')
    try {
      const res = await fetch('/api/session/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt }),
      })
      if (!res.ok) {
        const detail = await res.text()
        throw new Error(detail)
      }
      await refreshStatic()
    } catch (e) {
      setError(String(e))
      setStatus('idle')
      setPhase('idle')
    } finally {
      setBusy(false)
    }
  }

  async function linkTelegram() {
    setBusy(true)
    try {
      const res = await fetch('/api/telegram/discover', { method: 'POST' })
      const data = await res.json()
      if (!data.ok) throw new Error(data.hint || 'Send /start to the bot first')
      setError(null)
      alert(`Telegram linked (chat ${data.chat_id}). Test message ${data.test_message_sent ? 'sent' : 'failed'}.`)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function stopFlatten() {
    setBusy(true)
    try {
      await fetch('/api/session/stop', { method: 'POST' })
      setPaused(false)
      await refreshStatic()
    } finally {
      setBusy(false)
    }
  }

  async function togglePause() {
    setBusy(true)
    setError(null)
    try {
      const path = paused ? '/api/session/resume' : '/api/session/pause'
      const res = await fetch(path, { method: 'POST' })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || await res.text())
      setPaused(Boolean(data.paused))
      await refreshStatic()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function openLibrary() {
    setLibraryOpen(true)
    const res = await fetch('/api/library')
    const data = await res.json()
    setLibrary(data.days || [])
  }

  const running =
    status === 'running' ||
    status === 'preopen' ||
    status === 'open' ||
    status === 'closing' ||
    status === 'paused' ||
    status === 'starting' ||
    phase === 'starting' ||
    phase === 'parsing_brief' ||
    paused

  const canControl = running || busy

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <h1>Quack</h1>
          <span>multi-agent equity desk</span>
        </div>
        <div className="top-actions">
          <span className={`badge ${budget.paused ? 'budget-hit' : 'budget'}`} title={budget.pause_reason || 'Anthropic spend'}>
            AI ${budget.spent_usd.toFixed(2)} / ${budget.budget_usd.toFixed(0)}
          </span>
          <div className="budget-meter" title={`${budget.pct_used}% of Anthropic budget`}>
            <span style={{ width: `${Math.min(100, budget.pct_used)}%` }} />
          </div>
          <span className={`badge ${status === 'error' || phase === 'budget_paused' ? 'err' : 'live'}`}>
            {status} · {phase}
          </span>
          <button className="ghost" onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')} title="Toggle theme">
            {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
          </button>
          <button className="ghost" onClick={linkTelegram} disabled={busy} title="Link Telegram after /start">
            Telegram
          </button>
          <button className="ghost" onClick={openLibrary} title="Library">
            <BookOpen size={16} style={{ verticalAlign: 'middle', marginRight: 6 }} />
            Library
          </button>
        </div>
      </header>

      {(budgetAlert || budget.paused) && (
        <div className="budget-banner">
          {budgetAlert || budget.pause_reason || 'Anthropic budget limit reached (~$10). All operations paused.'}
        </div>
      )}

      <main className="layout">
        <section className="panel panel-brief" style={{ gridColumn: '1 / -1', animationDelay: '0.05s' }}>
          <header>
            <h2>Operator brief</h2>
          </header>
          <div className="body">
            <div className="run-form">
              <label>
                Prompt
                <textarea
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                  placeholder="Operator brief"
                  disabled={running}
                />
              </label>
              <div className="full run-actions">
                <button disabled={busy || running || prompt.trim().length < 3} onClick={start}>
                  <Play size={14} style={{ verticalAlign: 'middle', marginRight: 6 }} />
                  Run agents
                </button>
                <button
                  className={`pause ${paused ? 'is-paused' : ''}`}
                  disabled={busy || !canControl}
                  onClick={togglePause}
                  title={paused ? 'Resume agents from pause' : 'Pause agents, keep positions'}
                >
                  {paused ? (
                    <>
                      <PlayCircle size={14} style={{ verticalAlign: 'middle', marginRight: 6 }} />
                      Continue
                    </>
                  ) : (
                    <>
                      <Pause size={14} style={{ verticalAlign: 'middle', marginRight: 6 }} />
                      Stop agents
                    </>
                  )}
                </button>
                <button className="danger" disabled={!canControl} onClick={stopFlatten}>
                  <Square size={14} style={{ verticalAlign: 'middle', marginRight: 6 }} />
                  Stop & flatten
                </button>
                {error && <span style={{ color: 'var(--short)', fontSize: '0.85rem' }}>{error}</span>}
                {paused && (
                  <span style={{ color: 'var(--warn)', fontSize: '0.85rem' }}>
                    Agents paused — positions & logs kept
                  </span>
                )}
              </div>
            </div>
          </div>
        </section>

        <section className="panel panel-book" style={{ gridColumn: '1 / -1', animationDelay: '0.08s' }}>
          <header>
            <h2>Book & allocations</h2>
          </header>
          <div className="body">
            <div className="cash-grid">
              <div className="stat">
                <div className="label">Cash used</div>
                <div className="value">${portfolio.cash_used.toFixed(2)}</div>
              </div>
              <div className="stat">
                <div className="label">Cash unused</div>
                <div className="value">${portfolio.cash_unused.toFixed(2)}</div>
              </div>
              <div className="stat">
                <div className="label">Equity</div>
                <div className="value">${portfolio.total_equity.toFixed(2)}</div>
              </div>
              <div className="stat">
                <div className="label">Day PnL</div>
                <div className={`value ${portfolio.day_pnl >= 0 ? 'pos' : 'neg'}`}>
                  ${portfolio.day_pnl.toFixed(2)} ({portfolio.day_pnl_pct.toFixed(2)}%)
                </div>
              </div>
            </div>
            <div className="bars">
              {portfolio.positions.length === 0 && <div className="empty">—</div>}
              {portfolio.positions.map((p) => (
                <div className="bar-row" key={`${p.symbol}-${p.side}`} onClick={() => openSymbol(p.symbol)}>
                  <div className="sym">
                    {p.symbol}
                    <div className={p.side === 'long' ? 'side-long' : 'side-short'} style={{ fontSize: '0.68rem' }}>
                      {p.side}
                    </div>
                  </div>
                  <div className="bar-track">
                    <div
                      className={`bar-fill ${p.side}`}
                      style={{ width: `${Math.min(100, Math.max(4, p.allocation_pct * 100))}%` }}
                    />
                  </div>
                  <div className={`pnl ${p.unrealized_pnl >= 0 ? 'side-long' : 'side-short'}`}>
                    {(p.allocation_pct * 100).toFixed(1)}% · {p.unrealized_pnl_pct.toFixed(2)}%
                  </div>
                </div>
              ))}
            </div>
          </div>
        </section>

        <PerformanceCharts refreshKey={chartTick} liveSymbols={portfolio.positions.map((p) => p.symbol)} />

        <section className="panel panel-trades" style={{ animationDelay: '0.15s' }}>
          <header>
            <h2>Trades</h2>
          </header>
          <div className="body scrollable">
            <div className="trades-list">
              {trades.length === 0 && <div className="empty">—</div>}
              {trades.map((t) => (
                <div className="trade-item" key={t.id} onClick={() => setSelectedTrade(t)}>
                  <div className="title">
                    <span>
                      <span className={t.side === 'long' ? 'side-long' : 'side-short'}>{t.side.toUpperCase()}</span>{' '}
                      {t.symbol}
                    </span>
                    <span>{(t.allocation_pct * 100).toFixed(1)}%</span>
                  </div>
                  <div className="meta">
                    {t.status} · {t.qty.toFixed(4)} @ ${t.price.toFixed(2)} · {t.source}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="panel panel-log" style={{ animationDelay: '0.2s' }}>
          <header>
            <h2>Agent desk log</h2>
            <span style={{ color: 'var(--muted)', fontSize: '0.75rem' }}>{messages.length}</span>
          </header>
          <div className="body log-with-floor">
            <div className="log-list">
              {messages.length === 0 && <div className="empty">—</div>}
              {messages.map((m, i) => (
                <div className="log-item" key={m.id ?? `${m.ts}-${i}`} onClick={() => setSelectedMessage(m)}>
                  <div className="who">
                    {m.from_agent}
                    {m.to_agent ? ` → ${m.to_agent}` : ''}
                  </div>
                  <div className="txt">{m.content.length > 160 ? `${m.content.slice(0, 160)}…` : m.content}</div>
                  <div className="ts">{m.ts ? format(new Date(m.ts), 'HH:mm:ss') : ''}</div>
                </div>
              ))}
              <div ref={logEnd} />
            </div>
            <FloorFeed floor={floor} />
          </div>
        </section>
      </main>

      <section className="sapra">
        <div className="title">
          <span>Floor</span>
        </div>
        <div className="thread">
          {sapraThread.length === 0 && <div className="empty">—</div>}
          {sapraThread.map((b, i) => (
            <div className="bubble" key={i}>
              <div className="who">{b.role === 'you' ? 'You' : 'Floor'}</div>
              <div>{b.text}</div>
            </div>
          ))}
          <div ref={sapraEnd} />
        </div>
        <div className="composer">
          <input
            value={sapraInput}
            onChange={(e) => setSapraInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') askSapra()
            }}
            placeholder="Ask Floor"
            disabled={sapraBusy}
          />
          <button disabled={sapraBusy || !sapraInput.trim()} onClick={askSapra}>
            Send
          </button>
        </div>
      </section>

      {selectedTrade && (
        <div className="modal-back" onClick={() => setSelectedTrade(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>
              {selectedTrade.side.toUpperCase()} {selectedTrade.symbol}
            </h3>
            <p style={{ color: 'var(--muted)' }}>{selectedTrade.reason}</p>
            <div className="detail-card">
              <h4>Fill</h4>
              <div>
                {selectedTrade.qty.toFixed(4)} @ ${selectedTrade.price.toFixed(2)} · {(selectedTrade.allocation_pct * 100).toFixed(1)}% ·{' '}
                {selectedTrade.source} · {selectedTrade.status}
              </div>
            </div>
            <pre>{JSON.stringify(selectedTrade, null, 2)}</pre>
            <button
              className="ghost"
              onClick={() => {
                const sym = selectedTrade.symbol
                setSelectedTrade(null)
                openSymbol(sym)
              }}
            >
              Open symbol dossier
            </button>{' '}
            <button className="ghost" onClick={() => setSelectedTrade(null)}>
              Close
            </button>
          </div>
        </div>
      )}

      {selectedMessage && (
        <div className="modal-back" onClick={() => setSelectedMessage(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>
              {selectedMessage.from_agent}
              {selectedMessage.to_agent ? ` → ${selectedMessage.to_agent}` : ''}
            </h3>
            <p style={{ color: 'var(--muted)', fontFamily: 'var(--mono)', fontSize: '0.75rem' }}>
              {selectedMessage.kind} · {selectedMessage.ts}
            </p>
            <p style={{ whiteSpace: 'pre-wrap' }}>{selectedMessage.content}</p>
            <pre>{JSON.stringify(selectedMessage, null, 2)}</pre>
            <button className="ghost" onClick={() => setSelectedMessage(null)}>
              Close
            </button>
          </div>
        </div>
      )}

      {symbolDetail && (
        <div className="modal-back" onClick={() => setSymbolDetail(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ width: 'min(860px, 100%)' }}>
            <h3>{String(symbolDetail.symbol)} dossier</h3>
            <p style={{ color: 'var(--muted)' }}>{String(symbolDetail.motivation || 'No Strategist motivation on file yet.')}</p>
            <div className="detail-grid">
              <div className="detail-card">
                <h4>Position</h4>
                <pre>{JSON.stringify(symbolDetail.position, null, 2)}</pre>
              </div>
              <div className="detail-card">
                <h4>Plan trade</h4>
                <pre>{JSON.stringify(symbolDetail.plan_trade, null, 2)}</pre>
              </div>
              <div className="detail-card">
                <h4>Agent views / scores</h4>
                <pre>{JSON.stringify(symbolDetail.agent_views, null, 2)}</pre>
              </div>
              <div className="detail-card">
                <h4>Trades</h4>
                <pre>{JSON.stringify(symbolDetail.trades, null, 2)}</pre>
              </div>
            </div>
            <button className="ghost" onClick={() => setSymbolDetail(null)}>
              Close
            </button>
          </div>
        </div>
      )}

      {libraryOpen && (
        <div className="modal-back" onClick={() => { setLibraryOpen(false); setLibraryDay(null) }}>
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ width: 'min(860px, 100%)' }}>
            <h3>Library</h3>
            {!libraryDay ? (
              <div className="library-grid">
                {library.length === 0 && <div className="empty">No saved trading days yet.</div>}
                {library.map((d) => (
                  <div className="lib-card" key={d.id} onClick={() => setLibraryDay(d)}>
                    <div className="d">
                      {d.trade_date}
                      {d.simulated_after_hours || (d.full_report as { simulated_after_hours?: boolean })?.simulated_after_hours ? (
                        <span className="sim-tag"> after-hours sim · not T212</span>
                      ) : null}
                    </div>
                    <div className={`p ${d.total_pnl >= 0 ? 'side-long' : 'side-short'}`}>
                      ${d.total_pnl.toFixed(2)} ({d.total_pnl_pct.toFixed(2)}%) · best {d.best_symbol || '—'} · worst{' '}
                      {d.worst_symbol || '—'}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div>
                <button className="ghost" onClick={() => setLibraryDay(null)} style={{ marginBottom: 12 }}>
                  ← Back
                </button>
                <h3>{libraryDay.trade_date}</h3>
                {(libraryDay.simulated_after_hours ||
                  (libraryDay.full_report as { simulated_after_hours?: boolean })?.simulated_after_hours) && (
                  <div className="sim-banner">
                    {libraryDay.execution_note ||
                      (libraryDay.full_report as { execution_note?: string })?.execution_note ||
                      'AFTER-HOURS SIMULATION — not Trading212. Fills were simulated on yfinance marks, not live T212 executions.'}
                  </div>
                )}
                <p style={{ whiteSpace: 'pre-wrap' }}>{libraryDay.conclusions}</p>
                <p>
                  <strong>Pros:</strong> {(libraryDay.pros || []).join(' · ')}
                </p>
                <p>
                  <strong>Cons:</strong> {(libraryDay.cons || []).join(' · ')}
                </p>
                <p>
                  <strong>Mistakes:</strong> {(libraryDay.mistakes || []).join(' · ')}
                </p>
                <h4>Agent scores</h4>
                <pre>{JSON.stringify(libraryDay.agent_scores, null, 2)}</pre>
                <h4>Full report</h4>
                <pre>{JSON.stringify(libraryDay.full_report, null, 2)}</pre>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
