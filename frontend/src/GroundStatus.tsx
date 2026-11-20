import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { ApiError, type OpsApi, type Selection } from './api'
import { Button } from './components/ui'
import { Empty, Facts, ToolPanel, ToolRow, ToolTabs } from './OpsCommon'
import type { GroundDetail, GroundEntry, Process, Turn } from './ops-types'
import { Resource, utcStamp, utcTime } from './presentation'

const lanes = [['active', 'Active'], ['recent', 'Recent'], ['clarify', 'Verify']] as const
const processes: [Process, string][] = [
  ['fuel', 'Fuel'], ['handling', 'Handling'], ['cabin', 'Cabin'],
  ['pax', 'Passengers'], ['documents', 'Documents'], ['other', 'Other'],
]
const events: Record<string, string> = {
  IN_BLOCK: 'In-block', OUT_BLOCK: 'Off-block', LANDING: 'Landing', TAKEOFF: 'Takeoff', STAND: 'Stand',
  CHOCKS_ON: 'Chocks on', CHOCKS_OFF: 'Chocks off',
  PAX_OFF_STARTED: 'Deplaning started', PAX_OFF_COMPLETED: 'Deplaning complete',
  FUEL_REQUESTED: 'Fuel requested', FUEL_TRUCK_ARRIVED: 'Fuel truck arrived',
  FUEL_STARTED: 'Fuel started', FUEL_COMPLETED: 'Fuel complete',
  CABIN_READY: 'Cabin ready', HANDLING_READY: 'Handling ready', DOCUMENTS_READY: 'Documents ready',
  BOARDING_STARTED: 'Boarding started', BOARDING_COMPLETED: 'Boarding complete',
  PAX_ON_BOARD: 'Passengers on board', REPORTED_READY: 'Reported ready',
  BLOCKER_OPEN: 'Open blocker', BLOCKER_CLOSE: 'Close blocker', TARGET: 'Set UTC target', NOTE: 'Station note',
}

function TurnRow({ turn, onTurn, prefix = '' }: { turn: Turn; onTurn: (turn: Turn) => void; prefix?: string }) {
  const flight = [turn.inbound_number, turn.outbound_number].filter(Boolean).join(' → ')
  return <ToolRow title={prefix + [turn.station, turn.registration].filter(Boolean).join(' · ')}
    detail={[flight, utcTime(turn.planned_in) + ' / ' + utcTime(turn.planned_out)].filter(Boolean).join(' · ')}
    action={turn.status.toUpperCase()} onClick={() => onTurn(turn)} />
}

export function GroundStatus({ api, selection, onTurn }: {
  api: OpsApi; selection?: Selection; onTurn: (turn: Turn) => void
}) {
  const [lane, setLane] = useState<Turn['status']>('active')
  return (
    <>
      <ToolTabs label="Ground Status lane" options={lanes} active={lane} onChange={setLane} />
      <Resource load={api.ground} label="Ground Status">
        {(data, refresh, refreshing) => {
          const rows = data.cards.filter((turn) => turn.status === lane)
          const flight = selection?.flight
          const matches = flight ? data.cards.filter((turn) =>
            turn.inbound_id === flight.id || turn.outbound_id === flight.id) : []
          return (
            <ToolPanel title="Ground turns" source="Leon schedule · OPS journal" state={data} refresh={refresh} refreshing={refreshing}>
              {flight && (
                <div className="tool-section">
                  <h3 className="caption">{flight.number ?? 'Selected flight'} · Ground turns</h3>
                  {matches.length ? matches.map((turn) => (
                    <TurnRow key={turn.key} turn={turn} onTurn={onTurn}
                      prefix={turn.outbound_id === flight.id ? 'DEP · ' : 'ARR · '} />
                  )) : <Empty>No turn for the selected leg in the current window.</Empty>}
                </div>
              )}
              <h3 className="px-3 pt-3 caption">All turns · ±6h UTC</h3>
              {rows.length ? rows.map((turn) => <TurnRow key={turn.key} turn={turn} onTurn={onTurn} />) :
                <Empty>{data.status === 'PARTIAL' ? 'No turns in this lane in the partial schedule.' : 'No turns in this lane.'}</Empty>}
            </ToolPanel>
          )
        }}
      </Resource>
    </>
  )
}

function inputTime(value: FormDataEntryValue | null) {
  return typeof value === 'string' && value ? new Date(value + 'Z').toISOString() : undefined
}

function EventForm({ turn, api, onSaved }: { turn: Turn; api: OpsApi; onSaved: (detail: GroundDetail) => void }) {
  const [kind, setKind] = useState('IN_BLOCK')
  const [message, setMessage] = useState('')
  const [saving, setSaving] = useState(false)
  const request = useRef<AbortController | null>(null)
  const retry = useRef<{ fingerprint: string; id: string } | null>(null)
  const processRequired = kind === 'BLOCKER_OPEN' || kind === 'BLOCKER_CLOSE'
  const textRequired = kind === 'NOTE' || kind === 'STAND' || kind === 'BLOCKER_OPEN'
  useEffect(() => () => request.current?.abort(), [])

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (request.current) return
    const form = new FormData(event.currentTarget)
    const entry = {
      kind, event_at: inputTime(form.get('event_at')),
      target_at: inputTime(form.get('target_at')), expected_at: inputTime(form.get('expected_at')),
      process: (form.get('process') || undefined) as Process | undefined,
      text: String(form.get('text') ?? '').trim(), critical: form.get('critical') === 'on',
    }
    const fingerprint = JSON.stringify(entry)
    if (retry.current?.fingerprint !== fingerprint) retry.current = { fingerprint, id: crypto.randomUUID() }
    const payload: GroundEntry = { ...entry, request_id: retry.current.id }
    const controller = new AbortController()
    request.current = controller
    setSaving(true)
    setMessage('')
    try {
      onSaved(await api.recordGround(turn.key, payload, controller.signal))
      if (controller.signal.aborted) return
      retry.current = null
      setMessage('Event recorded.')
    } catch (error) {
      if (!controller.signal.aborted) setMessage(error instanceof ApiError ? error.message : 'Connection unavailable. Retry this event.')
    } finally {
      request.current = null
      if (!controller.signal.aborted) setSaving(false)
    }
  }

  return (
    <form className="tool-form" onSubmit={submit}>
      <label>Event<select aria-label="Event" value={kind} onChange={(event) => { setKind(event.target.value); setMessage('') }}>
        {Object.entries(events).filter(([key]) => turn.outbound_id || !['OUT_BLOCK', 'TARGET'].includes(key))
          .map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select></label>
      <label>Event time UTC <span className="minor">Default now</span><input name="event_at" type="datetime-local" /></label>
      {processRequired && <label>Process<select required name="process">
        {processes.map(([value, text]) => <option value={value} key={value}>{text}</option>)}
      </select></label>}
      {kind === 'TARGET' && <label>Target UTC<input name="target_at" type="datetime-local" required /></label>}
      {kind === 'BLOCKER_OPEN' && <label>Expected clear UTC<input name="expected_at" type="datetime-local" /></label>}
      {(textRequired || kind === 'BLOCKER_OPEN') &&
        <label>{kind === 'STAND' ? 'Stand' : 'Details'}<textarea name="text" maxLength={kind === 'BLOCKER_OPEN' ? 160 : 500} required={textRequired} /></label>}
      {kind === 'NOTE' && <label className="checkbox-label"><input name="critical" type="checkbox" /> Safety-critical observation</label>}
      <Button type="submit" disabled={saving}>{saving ? 'Recording…' : 'Record event'}</Button>
      {message && <p className="minor" role="status">{message}</p>}
    </form>
  )
}

function CurrentTurn({ original, api, onStation, onFlight }: {
  original: GroundDetail; api: OpsApi; onStation: (code: string) => void; onFlight: () => void
}) {
  const [updated, setUpdated] = useState<{ base: GroundDetail; detail: GroundDetail }>()
  const [history, setHistory] = useState(false)
  const data = updated?.base === original ? updated.detail : original
  const { turn, timing } = data
  const loadHistory = useMemo(() => api.groundHistory(turn.key), [api, turn.key])
  return (
    <>
      <Facts entries={[
        ['IN plan', utcTime(timing.baseline_in)], ['OUT plan', utcTime(timing.baseline_out)],
        ['Reported IN', utcTime(timing.in)], ['Reported OUT', utcTime(timing.out)],
        ['Reported ready', utcTime(timing.ready)],
        [timing.target_source === 'Leon Schedule' ? 'Leon OUT target' : 'Internal target', utcTime(timing.target)],
        ['Ready forecast', utcTime(timing.forecast_ready)], ['Ready lower bound', utcTime(timing.ready_not_before)],
        ['Target risk', timing.risk_minutes === null ? null : timing.risk_minutes + ' min'],
        ['IN vs plan', timing.inbound_delta_minutes === null ? null : timing.inbound_delta_minutes + ' min'],
        ['OUT vs plan', timing.departure_delta_minutes === null ? null : timing.departure_delta_minutes + ' min'],
      ]} />
      {(timing.unknown_blockers || timing.blockers.length > 0) && <div className="tool-section">
        <h3 className="caption">Open blockers</h3>
        {timing.blockers.map((item) => <p className="tool-line" key={item.process}>
          {processes.find(([value]) => value === item.process)?.[1]} · {item.expected_at ? 'ETA ' + utcTime(item.expected_at) : 'ETA unreported'}{item.reason && ' · ' + item.reason}
        </p>)}
      </div>}
      {Object.entries(timing.movement_conflicts).map(([label, time]) =>
        <p className="tool-line text-amber-300" key={label}>{label} conflict · OPS {utcTime(time)} · Leon retained</p>)}
      <div className="tool-tabs px-3 pb-3">
        <Button variant="outline" onClick={() => onStation(turn.station)}>Station statistics</Button>
        <Button variant="outline" onClick={onFlight}>SKED</Button>
      </div>
      <details className="tool-disclosure" open><summary>Record ground event</summary>
        <EventForm turn={turn} api={api} onSaved={(detail) => setUpdated({ base: original, detail })} />
      </details>
      <details className="tool-disclosure" open><summary>Timeline · {data.events.length}</summary>
        {data.events.length ? <ol className="tool-lines">
          {[...data.events].reverse().map((row) => (
            <li className="tool-line" key={row.id}>
              <strong>{events[row.kind] ?? row.kind}</strong>
              <span className="minor"> · {utcStamp(row.event_at)} · {row.source === 'leon' ? 'Leon' : 'OPS'}</span>
              {(row.detail.text || row.detail.reason || row.detail.stand || row.detail.target_at || row.detail.process) && <p className="mt-1 break-words">
                {[row.detail.process, row.detail.text, row.detail.reason, row.detail.stand, row.detail.target_at && utcStamp(row.detail.target_at)].filter(Boolean).join(' · ')}
              </p>}
            </li>
          ))}
        </ol> : <Empty>No events recorded for this turn.</Empty>}
      </details>
      <details className="tool-disclosure" onToggle={(event) => setHistory(event.currentTarget.open)}>
        <summary>Historical turnaround samples</summary>
        {history && <Resource load={loadHistory} label="historical samples">
          {(historyData) => {
            const labels: Record<string, string> = {
              fuel_duration: 'Fuel uplift', fuel_wait: 'Fuel truck wait', ground_duration: 'On ground',
            }
            const rows = Object.entries(historyData.samples ?? {})
            const scope = (row: { station: string | null; aircraft_type: string | null }) =>
              (row.station ?? 'Fleet-wide') + ' · ' + (row.aircraft_type ?? 'All types')
            if (historyData.status === 'UNAVAILABLE') return <Empty>Ground history unavailable.</Empty>
            if (!rows.length && !historyData.blocker) return <Empty>At least 15 recorded turns are needed for a sample.</Empty>
            return <>
              {rows.map(([key, row]) => <p key={key} className="tool-line">
                {labels[key]} · {scope(row)} · {row.median_minutes} min median · P80 {row.p80_minutes} min · n={row.count}
              </p>)}
              {historyData.blocker && <p className="tool-line">
                Most recorded blocker: {historyData.blocker.process} · {scope(historyData.blocker)} · {historyData.blocker.count}/{historyData.blocker.turn_count} turns
              </p>}
            </>
          }}
        </Resource>}
      </details>
    </>
  )
}

export function GroundTurn({ api, turn, onStation, onFlight }: {
  api: OpsApi; turn: Turn; onStation: (code: string) => void; onFlight: () => void
}) {
  const load = useMemo(() => api.turn(turn.key), [api, turn.key])
  return <Resource load={load} label={turn.station}>
    {(data, refresh, refreshing) => <ToolPanel title={[turn.station, turn.registration].filter(Boolean).join(' · ')}
      source="Leon schedule · OPS journal" state={data} refresh={refresh} refreshing={refreshing}>
      <CurrentTurn original={data} api={api} onStation={onStation} onFlight={onFlight} />
    </ToolPanel>}
  </Resource>
}

export function GroundStation({ api, station }: { api: OpsApi; station: string }) {
  const load = useMemo(() => api.station(station), [api, station])
  return <Resource load={load} label={station + ' statistics'}>
    {(data, refresh, refreshing) => <ToolPanel title={station} source="OPS ground journal"
      state={data} refresh={refresh} refreshing={refreshing}>
      <Facts entries={[[`Turns / ${data.days ?? '—'}d`, data.turns], ['Recorded events', data.events]]} />
      {Object.entries(data.metrics ?? {}).map(([key, sample]) => <div className="tool-line" key={key}>
        <strong>{key}</strong> · {sample.count ? `median ${sample.median_minutes} min · P80 ${sample.p80_minutes} min · n=${sample.count}` : 'No paired events'}
      </div>)}
      {([['Safety observations', data.critical], ['Recent station notes', data.notes]] as const).map(([label, rows]) => (
        <details className="tool-disclosure" key={label}><summary>{label} · {rows?.length ?? 0}</summary>
          {rows?.map((row, index) => <p className="tool-line" key={index}>{row.date} · {row.text}</p>)}
        </details>
      ))}
    </ToolPanel>}
  </Resource>
}
