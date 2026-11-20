import { CalendarDays, ChartNoAxesCombined, ChevronLeft, ClipboardList, FolderOpen, PlaneTakeoff, Radar, RefreshCw, Search } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { Display, Flight, Handling, OpsApi, Plan, Selection } from './api'
import { Badge, Button } from './components/ui'
import { FleetDocs, OpsBrief } from './FleetTools'
import { GroundStation, GroundStatus, GroundTurn } from './GroundStatus'
import { ToolRow } from './OpsCommon'
import type { LegRef, Turn } from './ops-types'
import { Analytics, CharterCheck, CharterRequest, CharterSales } from './ReportingTools'
import { CrewPanel, HotelPanel, Preparation, WeatherPanel } from './Preparation'
import { flightAirport, flightTime, PanelHeader, Resource, StatusBadge, utcDate, utcTime } from './presentation'

export interface AppHost {
  bindBack?: (handler: () => void) => () => void
  openLink: (url: string) => void
}

const dateFormat = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'UTC', day: '2-digit', month: 'short', year: 'numeric',
})
const unknown = 'NOT VERIFIED'

function dayOffset(day: string, offset: number) {
  const date = new Date(day + 'T00:00:00Z')
  date.setUTCDate(date.getUTCDate() + offset)
  return utcDate(date.toISOString())
}

function Header({ title, back, mode, toggleMode }: {
  title: string; back?: () => void; mode?: Display; toggleMode?: () => void
}) {
  return (
    <header className="screen-header">
      <div className="flex min-w-0 items-center gap-3">
        {back ? (
          <Button variant="outline" size="icon" onClick={back} aria-label="Back"><ChevronLeft size={19} /></Button>
        ) : (
          <span className="brand-mark"><PlaneTakeoff size={23} aria-hidden="true" /></span>
        )}
        <div>
          <p className="caption">OPS Bot</p>
          <h1 className="text-2xl font-extrabold tracking-tight">{title}</h1>
        </div>
      </div>
      {toggleMode && (
        <Button variant="outline" onClick={toggleMode} className="font-mono text-xs" aria-label="Switch time and airport code display">
          {mode === 'LT' ? 'LT · IATA' : 'UTC · ICAO'}
        </Button>
      )}
    </header>
  )
}

export function OpenInTelegram() {
  return (
    <main className="app-frame">
      <Header title="OPS" />
      <section className="mx-4 mt-4 rounded-xl border border-border bg-card p-4">
        <h2 className="font-bold">Open in Telegram</h2>
        <p className="mt-1 text-sm text-muted-foreground">Select OPS in the bot menu.</p>
      </section>
    </main>
  )
}

const actions = [
  { view: 'ground', label: 'GROUND STATUS', detail: 'Turns & events', icon: Radar },
  { view: 'sked', label: 'SKED', detail: '7-day schedule', icon: CalendarDays },
  { view: 'brief', label: 'OPS BRIEF', detail: 'Fleet & stations', icon: ClipboardList },
  { view: 'docs', label: 'FLEET DOCS', detail: 'Docs & checklist', icon: FolderOpen },
  { view: 'analytics', label: 'ANALYTICS', detail: 'Schedule & logs', icon: ChartNoAxesCombined },
  { view: 'charter', label: 'CHARTER CHECK', detail: 'Fleet & history', icon: Search },
] as const

type Action = typeof actions[number]['view']
type Screen =
  | { view: 'dashboard' }
  | { view: Exclude<Action, 'docs'> }
  | { view: 'docs'; checklist?: boolean }
  | { view: 'card' | 'prep'; selection: Selection }
  | { view: 'linked'; leg: LegRef }
  | { view: 'ground-turn'; turn: Turn }
  | { view: 'ground-station'; station: string }
  | { view: 'charter-sales' }
  | { view: 'charter-request'; id: number }

const screenTitles: Partial<Record<Screen['view'], string>> = {
  dashboard: 'OPS Dashboard', card: 'Flight Card', prep: 'Preparation', linked: 'Flight Card',
  'ground-turn': 'Ground Turn', 'ground-station': 'Station Statistics',
  'charter-sales': 'Charter Sales', 'charter-request': 'Quote Request',
}

function Dashboard({ selection, mode, onOpen, onCard }: {
  selection?: Selection; mode: Display; onOpen: (view: Action) => void; onCard: (selection: Selection) => void
}) {
  return (
    <>
      <nav className="quick-actions" aria-label="OPS quick actions">
        {actions.map(({ view, label, detail, icon: Icon }) => (
          <Button key={view} variant="outline" onClick={() => onOpen(view)}
            className="min-h-22 min-w-0 flex-col items-start justify-between gap-1.5 px-2.5 py-2.5 text-left sm:px-3">
            <Icon size={20} className="text-primary" aria-hidden="true" />
            <span className="w-full">
              <span className="block font-mono text-[.7rem] font-bold sm:text-xs">{label}</span>
              <span className="mt-0.5 block text-[.69rem] leading-tight text-muted-foreground">{detail}</span>
            </span>
          </Button>
        ))}
      </nav>
      {selection && <div className="ops-panel">
        <ToolRow title={selection.flight.number ?? 'Selected flight'}
          detail={[selection.flight.aircraft, flightAirport(selection.flight, 'departure', mode) + ' → ' +
            flightAirport(selection.flight, 'arrival', mode), flightTime(selection.flight, 'std', mode)].filter(Boolean).join(' · ')}
          action="FLIGHT CARD" onClick={() => onCard(selection)} />
      </div>}
    </>
  )
}

function ResolvedCard({ selection, onSelect }: { selection: Selection; onSelect: (selection: Selection) => void }) {
  useEffect(() => onSelect(selection), [onSelect, selection])
  return null
}

function LinkedFlight({ api, leg, week, onSelect, onSked }: {
  api: OpsApi; leg: LegRef; week: string; onSelect: (selection: Selection) => void; onSked: () => void
}) {
  const day = leg.std?.slice(0, 10) ?? ''
  const first = day >= week && day <= dayOffset(week, 6) ? week : day
  const load = useCallback((signal: AbortSignal, refresh?: boolean) => api.schedule(first, dayOffset(first, 6), signal, refresh), [api, first])
  return <Resource load={load} label="confirmed SKED leg">
    {(data) => {
      const flight = data.flights.find((item) => item.id === leg.id && Date.parse(item.std) === Date.parse(leg.std ?? '') &&
        item.departure === leg.departure && item.arrival === leg.arrival)
      return flight
        ? <ResolvedCard selection={{ flight, week: first }} onSelect={onSelect} />
        : <section className="ops-panel p-3"><p className="mb-3 text-sm">Confirm the current leg in SKED.</p>
          <Button onClick={onSked}>Open SKED</Button></section>
    }}
  </Resource>
}

export function App({ api, host }: { api: OpsApi; host: AppHost }) {
  const [mode, setMode] = useState<Display>('UTC')
  const [week, setWeek] = useState(() => utcDate(new Date().toISOString()))
  const [day, setDay] = useState<string>()
  const [selection, setSelection] = useState<Selection>()
  const [stack, setStack] = useState<Screen[]>([{ view: 'dashboard' }])
  const screen = stack[stack.length - 1]
  const back = useCallback(() => setStack((current) => current.length > 1 ? current.slice(0, -1) : current), [])
  const open = useCallback((next: Screen) => setStack((current) => [...current, next]), [])
  const openSked = useCallback(() => open({ view: 'sked' }), [open])
  const openCard = useCallback((picked: Selection) => {
    setSelection(picked)
    open({ view: 'card', selection: picked })
  }, [open])
  const openLinked = useCallback((leg: LegRef) => {
    if (leg.std && leg.id) open({ view: 'linked', leg })
    else openSked()
  }, [open, openSked])
  const fromLinked = useCallback((picked: Selection) => {
    setSelection(picked)
    setStack((current) => current.at(-1)?.view === 'linked' ? [...current.slice(0, -1), { view: 'card', selection: picked }] : current)
  }, [])
  const view = screen.view
  useEffect(() => {
    window.scrollTo(0, 0)
    if (view !== 'dashboard') return host.bindBack?.(back)
  }, [screen, view, host, back])

  let content
  switch (screen.view) {
    case 'dashboard':
      content = <Dashboard selection={selection} mode={mode} onOpen={(action) => open({ view: action })} onCard={openCard} />
      break
    case 'sked':
      content = <Schedule api={api} mode={mode} week={week} day={day} onWeek={setWeek} onDay={setDay}
        onFlight={(flight) => openCard({ week, flight })} />
      break
    case 'card':
      content = <Card api={api} mode={mode} selection={screen.selection}
        onPrepare={() => open({ view: 'prep', selection: screen.selection })}
        onGround={() => { setSelection(screen.selection); open({ view: 'ground' }) }}
        onChecklist={() => { setSelection(screen.selection); open({ view: 'docs', checklist: true }) }} />
      break
    case 'prep':
      content = <Preparation api={api} mode={mode} selection={screen.selection} openLink={host.openLink} />
      break
    case 'linked':
      content = <LinkedFlight api={api} leg={screen.leg} week={week} onSelect={fromLinked} onSked={openSked} />
      break
    case 'ground':
      content = <GroundStatus api={api} selection={selection} onTurn={(turn) => open({ view: 'ground-turn', turn })} />
      break
    case 'ground-turn':
      content = <GroundTurn api={api} turn={screen.turn} onStation={(station) => open({ view: 'ground-station', station })} onFlight={openSked} />
      break
    case 'ground-station':
      content = <GroundStation api={api} station={screen.station} />
      break
    case 'brief':
      content = <OpsBrief api={api} selection={selection} onFlight={openLinked} />
      break
    case 'docs':
      content = <FleetDocs api={api} selection={selection} onSked={openSked} openLink={host.openLink} initial={screen.checklist ? 'checklist' : 'aircraft'} />
      break
    case 'analytics':
      content = <Analytics api={api} selection={selection} />
      break
    case 'charter':
      content = <CharterCheck api={api} selection={selection} onFlight={openLinked} onSales={() => open({ view: 'charter-sales' })} />
      break
    case 'charter-sales':
      content = <CharterSales api={api} onRequest={(id) => open({ view: 'charter-request', id })} />
      break
    case 'charter-request':
      content = <CharterRequest api={api} id={screen.id} />
      break
  }

  const chooseMode = view === 'sked' || view === 'card' || view === 'prep'
  return (
    <main className="app-frame">
      <Header title={screenTitles[view] ?? actions.find((action) => action.view === view)?.label ?? 'OPS'}
        back={view !== 'dashboard' && !host.bindBack ? back : undefined}
        mode={mode} toggleMode={chooseMode ? () => setMode((value) => value === 'UTC' ? 'LT' : 'UTC') : undefined} />
      <div className="screen-content">{content}</div>
    </main>
  )
}

function Schedule({
  api, mode, week, day, onWeek, onDay, onFlight,
}: {
  api: OpsApi; mode: Display; week: string; day?: string;
  onWeek: (day: string) => void; onDay: (day?: string) => void; onFlight: (flight: Flight) => void
}) {
  const end = dayOffset(week, 6)
  const load = useCallback((signal: AbortSignal, refresh?: boolean) =>
    api.schedule(week, end, signal, refresh),
  [api, week, end])
  function shift(days: number) {
    onWeek(dayOffset(week, days))
    onDay()
  }
  function select(value: string) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return
    if (value < week || value > end) onWeek(value)
    onDay(value)
  }
  return (
    <>
      <div className="week-controls flex items-center justify-between gap-2">
        <Button variant="outline" onClick={() => shift(-7)} aria-label="Previous seven days" className="font-mono text-xs">← 7 DAYS</Button>
        <span className="caption text-center">{week.slice(5)} / {end.slice(5)} UTC</span>
        <Button variant="outline" onClick={() => shift(7)} aria-label="Next seven days" className="font-mono text-xs">7 DAYS →</Button>
      </div>
      <div className="date-bar">
        <label className="date-control">
          <CalendarDays size={17} aria-hidden="true" />
          <span>{day ? dateFormat.format(new Date(day + 'T00:00:00Z')) : 'Select UTC date'}</span>
          <input aria-label="Schedule date (UTC)" type="date" value={day ?? week} onChange={(event) => select(event.target.value)} />
        </label>
        {day && <Button variant="ghost" onClick={() => onDay()}>All 7 days</Button>}
      </div>
      <Resource load={load} label="SKED">
        {(data, refresh, refreshing) => {
          const visible = data.flights.map((flight, index) => ({ flight, number: index + 1 }))
            .filter(({ flight }) => !day || flight.std.slice(0, 10) === day)
          return (
            <section className="ops-panel" aria-label="Scheduled flights">
              <div className="panel-bar">
                <div><span>{visible.length} {visible.length === 1 ? 'leg' : 'legs'}</span>
                  <p className="minor">Leon{data.fetched_at && ' · ' + utcTime(data.fetched_at)}</p>
                </div>
                <div className="flex items-center gap-1.5">
                  <StatusBadge status={data.status} until={data.fresh_until} />
                  <Button size="icon" variant="ghost" onClick={refresh} disabled={refreshing} aria-label="Refresh SKED"><RefreshCw size={16} /></Button>
                </div>
              </div>
              {!visible.length ? (
                <p className="px-3 py-5 text-sm text-muted-foreground">
                  {data.complete ? 'No flights scheduled.' : 'No flights in the partial schedule.'}
                </p>
              ) : (
                <>
                  <div className="strip strip-head" aria-hidden="true"><span>STD / STA</span><span>Flight / REG</span><span>DEP / ARR</span></div>
                  <ul className="divide-y divide-border">
                    {visible.map(({ flight, number }, index) => {
                      const utcDay = flight.std.slice(0, 10)
                      const showDay = !day && (index === 0 || visible[index - 1].flight.std.slice(0, 10) !== utcDay)
                      return <li key={flight.id}>
                        {showDay && <h2 className="border-b border-border px-3 py-1 caption">{dateFormat.format(new Date(flight.std))} UTC</h2>}
                        <button className="strip strip-flight" onClick={() => onFlight(flight)}>
                          <span className="flex flex-col font-mono text-xs">
                            <span className="text-primary">{flightTime(flight, 'std', mode)}</span>
                            <span className="text-muted-foreground">{flightTime(flight, 'sta', mode)}</span>
                          </span>
                          <span className="flex min-w-0 flex-col items-start">
                            <strong className="wrap-anywhere"><span className="minor">#{number}</span> {flight.number ?? unknown}</strong>
                            <span className="minor">{flight.aircraft ?? unknown}</span>
                          </span>
                          <span className="flex min-w-0 flex-col items-start">
                            <strong className="font-mono wrap-anywhere">{flightAirport(flight, 'departure', mode)} / {flightAirport(flight, 'arrival', mode)}</strong>
                            {flight.cancelled && <Badge variant="warning">Cancelled</Badge>}
                            {flight.ferry && <Badge>Ferry</Badge>}
                          </span>
                        </button>
                      </li>
                    })}
                  </ul>
                </>
              )}
            </section>
          )
        }}
      </Resource>
    </>
  )
}

function FlightLeg({ flight, mode }: { flight: Flight; mode: Display }) {
  const day = (mode === 'LT' ? flight.std_local : flight.std)?.slice(0, 10)
  return (
    <section className="ops-panel">
      <div className="panel-bar flex-wrap">
        <span>{flight.number ?? unknown} <span className="ml-1 text-muted-foreground">{flight.aircraft ?? unknown}{flight.aircraft_type ? ' / ' + flight.aircraft_type : ''}</span></span>
        <div className="flex flex-wrap gap-1">
          {flight.cancelled && <Badge variant="warning">Cancelled</Badge>}
          {flight.ferry && <Badge>Ferry</Badge>}
          <Badge>{day ? dateFormat.format(new Date(day + 'T00:00:00Z')) + ' ' + mode : unknown}</Badge>
        </div>
      </div>
      <dl className="grid grid-cols-2 divide-x divide-border">
        {([
          ['DEP', 'departure', 'STD', 'std'],
          ['ARR', 'arrival', 'STA', 'sta'],
        ] as const).map(([station, side, title, field]) => (
          <div key={station} className="min-w-0 space-y-1 p-3">
            <dt className="caption">{station}</dt>
            <dd className="font-mono text-xl font-semibold wrap-anywhere">{flightAirport(flight, side, mode)}</dd>
            <dt className="caption">{title}</dt>
            <dd className="font-mono text-sm text-primary">{flightTime(flight, field, mode)}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}

function PlanPanel({ data, mode }: { data: Plan; mode: Display }) {
  const ete = data.ete_minutes
  return (
    <section className="ops-panel" aria-label="Flight plan">
      <PanelHeader title="FLIGHT PLAN" source="ForeFlight" state={data} />
      <dl className="compact-fields">
        <dt>ETE</dt><dd>{ete === null ? 'UNAVAILABLE' : Math.floor(ete / 60) + 'h ' + String(ete % 60).padStart(2, '0') + 'm'}</dd>
        <dt>RTE</dt><dd>{data.route ?? 'UNAVAILABLE'}</dd>
        <dt>ALTN</dt><dd>{data.alternate ? data.alternate + (mode === 'LT' ? ' ICAO' : '') : unknown}</dd>
        <dt>OVERFLIGHT</dt><dd>{data.overflight?.length ? data.overflight.map((code) => code ?? unknown).join(' / ') : unknown}</dd>
      </dl>
    </section>
  )
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
  return (
    <section className="ops-panel">
      <PanelHeader title="HANDLING" source="Leon" state={data} />
      <div className="divide-y divide-border">
        {(['DEP', 'ARR'] as const).map((role) => {
          const handler = data.handlers.find((candidate) => candidate.role === role)
          return <div className="p-3 text-sm" key={role}>
            <p><Badge>{role}</Badge> {handler?.name ?? unknown}</p>
            {handler && <dl className="compact-fields px-0 pb-0">
              <dt>TEL</dt><dd><Contact value={handler.phone} kind="phone" /></dd>
              <dt>EMAIL</dt><dd><Contact value={handler.email} kind="email" /></dd>
            </dl>}
          </div>
        })}
      </div>
    </section>
  )
}

function Card({
  api, selection, mode, onPrepare, onGround, onChecklist,
}: { api: OpsApi; selection: Selection; mode: Display; onPrepare: () => void; onGround: () => void; onChecklist: () => void }) {
  const [refreshKey, setRefreshKey] = useState(0)
  const lastRefresh = useRef(0)
  const plan = useCallback((signal: AbortSignal, refresh?: boolean) => api.plan(selection, signal, refresh), [api, selection])
  const handling = useCallback((signal: AbortSignal, refresh?: boolean) => api.handling(selection, signal, refresh), [api, selection])
  const assignments = useCallback((signal: AbortSignal, refresh?: boolean) => api.assignments(selection, signal, refresh), [api, selection])
  const crew = useCallback((signal: AbortSignal, refresh?: boolean) => api.crew(selection, signal, refresh), [api, selection])
  const weather = useCallback((signal: AbortSignal, refresh?: boolean) => api.metar(selection, signal, refresh), [api, selection])

  return (
    <>
      <FlightLeg flight={selection.flight} mode={mode} />
      <div className="flex items-center gap-2">
        <Button className="flex-1" onClick={onPrepare}>Preparation</Button>
      </div>
      <div className="tool-tabs">
        <Button variant="outline" onClick={onGround}>Ground Status</Button>
        <Button variant="outline" onClick={onChecklist}>Flight checklist</Button>
        <Button variant="outline" size="icon" aria-label="Refresh Flight Card" onClick={() => {
          const now = Date.now()
          if (now - lastRefresh.current < 5000) return
          lastRefresh.current = now
          setRefreshKey((value) => value + 1)
        }}><RefreshCw size={16} /></Button>
      </div>
      <Resource load={plan} label="flight plan" refreshKey={refreshKey}>
        {(data) => <PlanPanel data={data} mode={mode} />}
      </Resource>
      <Resource load={assignments} label="crew assignments" refreshKey={refreshKey}>
        {(assigned, _refresh, refreshing) => (
          <>
            <Resource load={crew} label="FDP" refreshKey={refreshKey}
              pending={<CrewPanel data={assigned} assignments />}
            >
              {(data) => refreshing ? <CrewPanel data={assigned} assignments /> : <CrewPanel data={data} />}
            </Resource>
            <HotelPanel data={assigned} />
          </>
        )}
      </Resource>
      <Resource load={handling} label="handling" refreshKey={refreshKey}>
        {(data) => <HandlingPanel data={data} />}
      </Resource>
      <Resource load={weather} label="METAR" refreshKey={refreshKey}>
        {(data) => <WeatherPanel data={data} selection={selection} mode={mode} />}
      </Resource>
    </>
  )
}
