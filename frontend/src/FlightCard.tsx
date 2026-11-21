import { ChevronRight, ClipboardCheck, Plane, Radar, RefreshCw } from 'lucide-react'
import { useCallback, useRef, useState } from 'react'
import type { Display, Flight, Handling, OpsApi, Plan, Selection, SourceState } from './api'
import { Badge, Button } from './components/ui'
import { turnForFlight, type Turn } from './ops-types'
import { CrewPanel, HotelPanel, WeatherPanel } from './Preparation'
import { CopyButton, flightAirport, flightTime, PanelHeader, ResourceView, StatusBadge, useResource } from './presentation'

const dateFormat = new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', day: '2-digit', month: 'short', year: 'numeric' })
const unknown = 'NOT VERIFIED'

function duration(minutes: number | null | undefined) {
  return minutes == null ? null : Math.floor(minutes / 60) + 'h ' + String(minutes % 60).padStart(2, '0') + 'm'
}

function FlightLeg({ flight, mode, plan, checks, blockers }: {
  flight: Flight; mode: Display; plan?: Plan; checks: [string, SourceState | undefined][]; blockers: number
}) {
  const day = (mode === 'LT' ? flight.std_local : flight.std)?.slice(0, 10)
  return <section className="ops-panel">
    <header className="panel-bar">
      <span className="minor">{[flight.aircraft_type, day ? dateFormat.format(new Date(day + 'T00:00:00Z')) + ' ' + mode : null].filter(Boolean).join(' · ')}</span>
      <span className="flex flex-wrap justify-end gap-1">
        {flight.cancelled && <Badge variant="warning">CNL</Badge>}
        {flight.ferry && <Badge>FERRY</Badge>}
        {blockers > 0 && <Badge variant="warning">{blockers} GROUND {blockers === 1 ? 'BLOCKER' : 'BLOCKERS'}</Badge>}
      </span>
    </header>
    <div className="flight-hero">
      <div className="flight-end">
        <span>DEP · STD</span><strong>{flightAirport(flight, 'departure', mode)}</strong>
        <time>{flightTime(flight, 'std', mode)}</time>
      </div>
      <div className="flight-track">
        {duration(plan?.ete_minutes) && <Badge>ETE {duration(plan?.ete_minutes)}</Badge>}
        <span><Plane size={17} aria-hidden="true" /></span>
      </div>
      <div className="flight-end text-right">
        <span>ARR · STA</span><strong>{flightAirport(flight, 'arrival', mode)}</strong>
        <time>{flightTime(flight, 'sta', mode)}</time>
      </div>
    </div>
    <div className="flight-audit">
      {checks.map(([name, state]) => <div key={name}>
        <span>{name}</span>{state ? <StatusBadge status={state.status} until={state.fresh_until} /> : <span>—</span>}
      </div>)}
    </div>
  </section>
}

function PlanPanel({ data, mode }: { data: Plan; mode: Display }) {
  return <section className="ops-panel" aria-label="Flight plan">
    <PanelHeader title="FLIGHT PLAN" source="ForeFlight" state={data}
      action={data.route && <CopyButton text={data.route} label="Copy RTE" />} />
    <dl className="compact-fields">
      <dt>ETE</dt><dd>{duration(data.ete_minutes) ?? 'UNAVAILABLE'}</dd>
      <dt>RTE</dt><dd className="font-mono">{data.route ?? 'UNAVAILABLE'}</dd>
      <dt>ALTN</dt><dd>{data.alternate ? data.alternate + (mode === 'LT' ? ' ICAO' : '') : unknown}</dd>
      <dt>OVERFLIGHT</dt><dd>{data.overflight?.length ? data.overflight.map((code) => code ?? unknown).join(' / ') : unknown}</dd>
    </dl>
  </section>
}

function Contact({ value, kind }: { value: string | null; kind: 'phone' | 'email' }) {
  if (!value) return <span className="minor">{unknown}</span>
  const phone = value.replace(/[\s().-]/g, '')
  const href = kind === 'phone'
    ? /^\+?\d{5,18}$/.test(phone) && 'tel:' + phone
    : /^[\w.+-]+@[\w.-]+\.[a-z]{2,}$/i.test(value) && 'mailto:' + value
  return href ? <a href={href} className="text-primary underline">{value}</a> : <span>{value}</span>
}

function HandlingPanel({ data }: { data: Handling }) {
  return <section className="ops-panel">
    <PanelHeader title="HANDLING" source="Leon" state={data} />
    <div className="divide-y divide-border">
      {(['DEP', 'ARR'] as const).map((role) => {
        const handler = data.handlers.find((candidate) => candidate.role === role)
        return <div className="p-3 text-sm" key={role}>
          <p><Badge>{role}{handler && ' · ' + handler.station}</Badge> <strong>{handler?.name ?? unknown}</strong></p>
          {handler && <dl className="compact-fields px-0 pb-0">
            <dt>TEL</dt><dd><Contact value={handler.phone} kind="phone" /></dd>
            <dt>EMAIL</dt><dd><Contact value={handler.email} kind="email" /></dd>
          </dl>}
        </div>
      })}
    </div>
  </section>
}

export function FlightCard({ api, selection, mode, onPrepare, onGround, onChecklist }: {
  api: OpsApi; selection: Selection; mode: Display
  onPrepare: () => void; onGround: (turn?: Turn) => void; onChecklist: () => void
}) {
  const [refreshKey, setRefreshKey] = useState(0)
  const lastRefresh = useRef(0)
  const loadPlan = useCallback((signal: AbortSignal, refresh?: boolean) => api.plan(selection, signal, refresh), [api, selection])
  const loadHandling = useCallback((signal: AbortSignal, refresh?: boolean) => api.handling(selection, signal, refresh), [api, selection])
  const loadAssignments = useCallback((signal: AbortSignal, refresh?: boolean) => api.assignments(selection, signal, refresh), [api, selection])
  const loadCrew = useCallback((signal: AbortSignal, refresh?: boolean) => api.crew(selection, signal, refresh), [api, selection])
  const loadMetar = useCallback((signal: AbortSignal, refresh?: boolean) => api.metar(selection, signal, refresh), [api, selection])
  const plan = useResource(loadPlan, refreshKey)
  const handling = useResource(loadHandling, refreshKey)
  const assigned = useResource(loadAssignments, refreshKey)
  const crew = useResource(assigned.data && loadCrew, refreshKey)
  const metar = useResource(loadMetar, refreshKey)
  const ground = useResource(api.ground, refreshKey)
  const blockers = ground.data?.cards.find((turn) => turn.outbound_id === selection.flight.id && turn.status === 'active')?.blockers?.length ?? 0

  return <>
    <FlightLeg flight={selection.flight} mode={mode} plan={plan.data} blockers={blockers}
      checks={[['PLAN', plan.data], ['CREW', crew.data], ['WX', metar.data], ['NOTAM', undefined]]} />
    <Button className="prep-action justify-between" onClick={onPrepare}><strong>PREPARATION</strong><span>CREW · WX/TAF · NOTAM <ChevronRight size={16} /></span></Button>
    <div className="flight-actions">
      <Button variant="attention" onClick={() => onGround(turnForFlight(ground.data?.cards, selection.flight.id))}><Radar size={16} /> Ground Status</Button>
      <Button variant="outline" onClick={onChecklist}><ClipboardCheck size={16} /> Checklist</Button>
      <Button variant="outline" size="icon" aria-label="Refresh Flight Card" onClick={() => {
        const now = Date.now()
        if (now - lastRefresh.current < 5000) return
        lastRefresh.current = now
        setRefreshKey((count) => count + 1)
      }}><RefreshCw size={16} /></Button>
    </div>
    <ResourceView result={plan} label="flight plan">{(data) => <PlanPanel data={data} mode={mode} />}</ResourceView>
    <ResourceView result={assigned} label="crew assignments">
      {(data, _refresh, refreshing) => <section className="ops-panel">
        <details className="tool-disclosure">
          <summary className="section-kicker">Crew · FDP &amp; hotel</summary>
          <ResourceView result={crew} label="FDP" pending={<CrewPanel data={data} assignments />}>
            {(duty) => refreshing ? <CrewPanel data={data} assignments /> : <CrewPanel data={duty} />}
          </ResourceView>
          <HotelPanel data={data} />
        </details>
      </section>}
    </ResourceView>
    <ResourceView result={handling} label="handling">{(data) => <HandlingPanel data={data} />}</ResourceView>
    <ResourceView result={metar} label="METAR">{(data) => <WeatherPanel data={data} selection={selection} mode={mode} />}</ResourceView>
  </>
}
