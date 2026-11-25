import { useCallback, useState, type FormEvent } from 'react'
import type { Load, OpsApi, Selection } from './api'
import { Button } from './components/ui'
import { Empty, Facts, ToolPanel, ToolTabs } from './OpsCommon'
import type { Journey, JourneyDays, JourneyFilters, Planned, PlannedPeriod, Priorities } from './ops-types'
import { Resource, utcStamp } from './presentation'

const ranges: [PlannedPeriod, string][] = [['7d', '7 days'], ['1m', '1 month'], ['3m', '3 months'], ['6m', '6 months'], ['12m', '12 months']]
const emptyFilters: JourneyFilters = { departure: '', arrival: '', aircraft_type: '' }
const analyticsModes = [['planned', 'Leon schedule'], ['journey', 'Journey Log']] as const
const plannedViews = [['management', 'Overview'], ['commercial', 'Commercial'], ['finance', 'Finance'], ['timings', 'Timings']] as const
const analyticsPriorities: Record<string, string> = {
  block: 'Compare reported block time and schedule for the same route and aircraft type.',
  fuel: 'Check reported fuel burn, uplift and units. Reconcile charges with invoices.',
  geography: 'Review route, airport and aircraft type coverage.',
  ferry: 'Check confirmed ferry legs and investigate unclassified records.',
  cancellations: 'Review cancellations and confirm causes and replacement legs.',
  schedule: 'Check incomplete logs and compare planned and actual coverage.',
  performance: 'Match archived ForeFlight performance to the same reported flight and aircraft.',
  sales: 'Link quote variants to requests and recorded outcomes.',
  crew: 'Confirm crew availability and capture additional crew costs.',
  handling: 'Reconcile ordered ground and cabin services with recorded delivery.',
  margin: 'Link quotes, flight records and invoices to measure actual margin.',
}
const min = (value?: number | null) => value == null ? undefined : value + ' min'
const hours = (value?: number | null) => value == null ? undefined : (value / 60).toFixed(1) + ' h'

function dateRange(data: { start?: string; end?: string }) {
  return [data.start, data.end].filter(Boolean).join(' → ')
}

function Rankings({ label, rows }: { label: string; rows?: { label: string; value: number | string }[] }) {
  if (!rows?.length) return null
  return <details className="tool-disclosure"><summary>{label}</summary>
    <Facts entries={rows.map((row) => [row.label, row.value])} />
  </details>
}

function PriorityReview({ load, labels }: { load: Load<Priorities>; labels: Record<string, string> }) {
  const [open, setOpen] = useState(false)
  return <details className="tool-disclosure" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>Review priorities</summary>
    {open && <Resource load={load} label="review priorities">
      {(data) => {
        const selected = data.priorities?.map((id) => labels[id]).filter(Boolean)
        return selected?.length ? <ol>{selected.map((label) => <li className="tool-line" key={label}>{label}</li>)}</ol> :
          <Empty>Priority review unavailable.</Empty>
      }}
    </Resource>}
  </details>
}

function PlannedFacts({ data, view }: { data: Planned; view: typeof plannedViews[number][0] }) {
  if (view === 'finance') return <>
    <Empty>Revenue, margin and operating costs are unavailable from Leon schedule.</Empty>
    <Facts entries={[[ 'Scheduled hours', hours(data.minutes)], ['Confirmed ferry hours', hours(data.ferry_minutes)],
      ['Confirmed non-ferry hours', hours(data.nonferry_minutes)], ['Ferry status unknown', data.unknown_ferry]]} />
    <Rankings label="Hours by registration" rows={Object.entries(data.hours_by_tail ?? {}).map(([label, value]) => ({ label, value: hours(value) ?? '—' }))} />
  </>
  if (view === 'commercial') return <>
    <Facts entries={[[ 'Confirmed non-ferry', data.nonferry_count], ['Confirmed ferry', data.ferry_count],
      ['Ferry status unknown', data.unknown_ferry], ['Ferry missing duration', data.ferry_missing_duration]]} />
    <Rankings label="Routes" rows={data.routes} />
    <Rankings label="Ferry routes" rows={data.ferry_routes} />
    <Rankings label="Destinations" rows={data.destinations} />
  </>
  if (view === 'timings') return <>
    <Facts entries={[[ 'Average block', min(data.average)], ['Median block', min(data.median)],
      ['Turns evaluated', data.turn_count], ['Idle ≥72h', data.idle_72h], ['Idle hours', hours(data.idle_minutes)],
      ['Schedule overlaps', data.overlap_count], ['Position mismatches', data.position_mismatch_count]]} />
    <Rankings label="Idle hours by registration" rows={data.idle_by_tail?.map(({ label, value }) => ({ label, value: hours(value) ?? '—' }))} />
  </>
  return <>
    <Facts entries={[[ 'Scheduled legs', data.legs], ['Cancelled', data.cancelled], ['Unassigned', data.unassigned],
      ['Excluded records', data.excluded], ['Total block hours', hours(data.minutes)], ['Known durations', data.known_durations]]} />
    <Rankings label="Legs by registration" rows={data.tails} />
    <Rankings label="Legs by month" rows={Object.entries(data.legs_by_month ?? {}).sort().map(([label, value]) => ({ label, value }))} />
  </>
}

function PlannedAnalytics({ api }: { api: OpsApi }) {
  const [period, setPeriod] = useState<PlannedPeriod>('7d')
  const [view, setView] = useState<typeof plannedViews[number][0]>('management')
  const load = useCallback((signal: AbortSignal, refresh?: boolean) => api.planned(period, signal, refresh), [api, period])
  return <>
    <div className="tool-form ops-panel">
      <label>Period<select value={period} onChange={(event) => setPeriod(event.target.value as PlannedPeriod)}>
        {ranges.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
      </select></label>
    </div>
    <ToolTabs label="Planned analytics view" options={plannedViews} active={view} onChange={setView} />
    <Resource key={period} load={load} label="schedule analytics">
      {(data, refresh, refreshing) => <ToolPanel title="Planned activity" source="Leon schedule" state={data} refresh={refresh} refreshing={refreshing}>
        <p className="tool-line minor">{dateRange(data)} · {data.complete_windows} / {data.total_windows} windows complete</p>
        <PlannedFacts data={data} view={view} />
      </ToolPanel>}
    </Resource>
  </>
}

function JourneyFacts({ data }: { data: Journey }) {
  return <>
    <Facts entries={[[ 'Scheduled records', data.scheduled_count], ['Reported actual', data.actual_count],
      ['Log open', data.open_log_count], ['Log missing', data.missing_log_count], ['Log status unknown', data.unknown_log_count],
      ['Cancelled', data.cancelled_count], ['Reported ferry', data.reported_ferry_count], ['Ferry status unknown', data.reported_unknown_ferry_count],
      ['Mean block', min(data.block_minutes?.mean)], ['Mean airborne', min(data.airborne_minutes?.mean)],
      ['Mean block vs plan', min(data.block_delta_minutes?.mean)], ['Block duration samples', data.block_minutes?.count],
      ['Mean distance', data.distance_nm?.mean == null ? undefined : data.distance_nm.mean + ' NM'],
    ]} />
    <Facts entries={[
      ...Object.entries(data.fuel ?? {}).map(([unit, sample]): [string, string] =>
        ['Mean fuel burn', (sample.mean ?? '—') + ' ' + unit + ' · n=' + sample.count]),
      ...Object.entries(data.fuel_uplift ?? {}).map(([unit, sample]): [string, string] =>
        ['Mean uplift', (sample.mean ?? '—') + ' ' + unit + ' · n=' + sample.count]),
    ]} />
    <Rankings label="Reported routes" rows={data.routes?.map((row) => ({ label: row.departure + ' → ' + row.arrival, value: row.count }))} />
    <Rankings label="Airports" rows={data.airports?.map((row) => ({ label: row.icao, value: row.count }))} />
    <Rankings label="Aircraft types" rows={data.aircraft_types?.map((row) => ({ label: row.type, value: row.count }))} />
  </>
}

function JourneyAnalytics({ api, selection }: { api: OpsApi; selection?: Selection }) {
  const [days, setDays] = useState<JourneyDays>(7)
  const [form, setForm] = useState(emptyFilters)
  const [filters, setFilters] = useState(emptyFilters)
  const [error, setError] = useState('')
  const load = useCallback((signal: AbortSignal, refresh?: boolean) => api.journey(days, filters, signal, refresh), [api, days, filters])
  const priorities = useCallback((signal: AbortSignal, refresh?: boolean) =>
    api.journeyPriorities(days, filters, false, signal, refresh), [api, days, filters])

  function filter(event: FormEvent) {
    event.preventDefault()
    if (Boolean(form.departure) !== Boolean(form.arrival)) {
      setError('Enter both DEP and ARR.')
      return
    }
    setError('')
    setFilters(form)
  }

  return <>
    <form className="ops-panel tool-form" onSubmit={filter} aria-label="Journey Log filters">
      <label>Period<select value={days} onChange={(event) => setDays(Number(event.target.value) as JourneyDays)}>
        {[7, 30, 90, 365].map((day) => <option key={day} value={day}>{day} days</option>)}
      </select></label>
      <div className="grid grid-cols-2 gap-2">
        <label>DEP ICAO<input value={form.departure} maxLength={4} pattern="[A-Z0-9]{4}" onChange={(event) => setForm({ ...form, departure: event.target.value.toUpperCase() })} /></label>
        <label>ARR ICAO<input value={form.arrival} maxLength={4} pattern="[A-Z0-9]{4}" onChange={(event) => setForm({ ...form, arrival: event.target.value.toUpperCase() })} /></label>
      </div>
      <label>Aircraft type<input value={form.aircraft_type} maxLength={4} pattern="[A-Z0-9]{2,4}" onChange={(event) => setForm({ ...form, aircraft_type: event.target.value.toUpperCase() })} /></label>
      <div className="tool-tabs">
        <Button type="submit">Apply filter</Button>
        {selection?.flight.departure && selection.flight.arrival && <Button type="button" variant="outline" onClick={() => {
          const { departure, arrival, aircraft_type } = selection.flight
          if (!departure || !arrival) return
          const selected = { departure, arrival, aircraft_type: aircraft_type && /^[A-Z0-9]{2,4}$/.test(aircraft_type) ? aircraft_type : '' }
          setError('')
          setForm(selected)
          setFilters(selected)
        }}>Selected route</Button>}
        {filters !== emptyFilters && <Button type="button" variant="ghost" onClick={() => { setForm(emptyFilters); setFilters(emptyFilters); setError('') }}>Clear</Button>}
      </div>
      {error && <p className="minor" role="alert">{error}</p>}
    </form>
    <Resource key={JSON.stringify([days, filters])} load={load} label="Journey Log">
      {(data, refresh, refreshing) => <ToolPanel title={filters.departure ? filters.departure + ' → ' + filters.arrival : 'Reported activity'}
        source="Leon Journey Log" state={data} refresh={refresh} refreshing={refreshing}>
        <p className="tool-line minor">{dateRange(data)} · {data.covered_days} / {days} days covered
          {data.last_synced_at && ' · Synced ' + utcStamp(data.last_synced_at)}</p>
        <JourneyFacts data={data} />
        <PriorityReview load={priorities} labels={analyticsPriorities} />
      </ToolPanel>}
    </Resource>
  </>
}

export function Analytics({ api, selection }: { api: OpsApi; selection?: Selection }) {
  const [mode, setMode] = useState<typeof analyticsModes[number][0]>('planned')
  return <>
    <ToolTabs label="Analytics source" options={analyticsModes} active={mode} onChange={setMode} />
    {mode === 'planned' ? <PlannedAnalytics api={api} /> : <JourneyAnalytics api={api} selection={selection} />}
  </>
}
