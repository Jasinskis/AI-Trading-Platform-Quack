type FloorAgent = { agent: string; task: string }
type FloorState = {
  last: FloorAgent | null
  current: FloorAgent[]
  next: FloorAgent[]
  meeting: { members: string[]; topic: string } | null
}

export default function FloorFeed({ floor }: { floor: FloorState }) {
  const meeting = floor.meeting
  const current = floor.current || []
  const next = floor.next || []
  const last = floor.last

  return (
    <div className="floor-feed">
      <div className="floor-label">Floor</div>
      <div className="floor-map">
        <div className={`floor-node last ${last ? '' : 'empty-node'}`}>
          <div className="floor-role">Last</div>
          <div className="floor-name">{last?.agent || '—'}</div>
          {last?.task && <div className="floor-task">{last.task}</div>}
        </div>

        <div className="floor-arrow solid" aria-hidden>
          →
        </div>

        {meeting ? (
          <div className="floor-node meeting current">
            <div className="floor-role">Meeting in progress</div>
            <div className="floor-name">{meeting.members.join(' · ')}</div>
            <div className="floor-task">{meeting.topic}</div>
          </div>
        ) : (
          <div className={`floor-node current ${current.length ? '' : 'empty-node'}`}>
            <div className="floor-role">Now</div>
            {current.length === 0 && <div className="floor-name">Idle</div>}
            {current.map((c) => (
              <div key={c.agent} className="floor-current-item">
                <div className="floor-name">{c.agent}</div>
                <div className="floor-task">{c.task}</div>
              </div>
            ))}
          </div>
        )}

        <div className="floor-arrow blur" aria-hidden>
          →
        </div>

        <div className={`floor-node next ${next.length ? '' : 'empty-node'}`}>
          <div className="floor-role">Next</div>
          {next.length === 0 && <div className="floor-name">—</div>}
          {next.map((n) => (
            <div key={n.agent + n.task} className="floor-current-item">
              <div className="floor-name">{n.agent}</div>
              <div className="floor-task">{n.task}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
