import { useMemo, useState } from 'react'
import type { OpsApi, Selection } from './api'
import { Button } from './components/ui'
import { Empty, Facts, ToolPanel, ToolRow, ToolTabs } from './OpsCommon'
import type { GroundDetail, Turn } from './ops-types'
import { eventLabels, GroundTurnControls } from './GroundTurnControls'
import { Resource, utcStamp, utcTime } from './presentation'

const lanes = [['active', 'Active'], ['recent', 'Recent'], ['clarify', 'Verify']] as const
function TurnRow({ turn, onTurn, prefix = '' }: { turn: Turn; onTurn: (turn: Turn) => void; prefix?: string }) {
  const flight = [turn.inbound_number, turn.outbound_number].filter(Boolean).join(' → ')
  const blockers = turn.blockers?.length ?? 0
  return <ToolRow title={prefix + [turn.station, turn.registration].filter(Boolean).join(' · ')}
    detail={[flight, utcTime(turn.planned_in) + ' / ' + utcTime(turn.planned_out)].filter(Boolean).join(' · ')}
    action={blockers ? blockers + ' BLOCKER' + (blockers === 1 ? '' : 'S') : turn.status.toUpperCase()} onClick={() => onTurn(turn)} />
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
      <GroundTurnControls data={data} api={api} onSaved={(detail) => setUpdated({ base: original, detail })} />
      <details className="tool-disclosure"><summary>Timing &amp; forecast</summary>
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
      {Object.entries(timing.movement_conflicts).map(([label, time]) =>
        <p className="tool-line text-amber-300" key={label}>{label} conflict · OPS {utcTime(time)} · Leon retained</p>)}
      </details>
      <div className="tool-tabs px-3 pb-3">
        <Button variant="outline" onClick={() => onStation(turn.station)}>Station statistics</Button>
        <Button variant="outline" onClick={onFlight}>SKED</Button>
      </div>
      <details className="tool-disclosure" open><summary>Timeline · {data.events.length}</summary>
        {data.events.length ? <ol className="tool-lines">
          {[...data.events].reverse().map((row) => (
            <li className="tool-line" key={row.id}>
              <strong>{eventLabels[row.kind] ?? row.kind}</strong>
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
