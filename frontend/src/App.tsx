import { CalendarDays, ChevronLeft, PlaneTakeoff, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { Display, Flight, Handling, OpsApi, Plan, Selection } from './api'
import { Badge, Button } from './components/ui'
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
      <Header title="SKED" />
      <section className="mx-4 mt-4 rounded-xl border border-border bg-card p-4">
        <h2 className="font-bold">Open in Telegram</h2>
        <p className="mt-1 text-sm text-muted-foreground">Select SKED in the OPS Bot menu.</p>
      </section>
    </main>
  )
}

type Screen = { view: 'sked' } | { view: 'card' | 'prep'; selection: Selection }

export function App({ api, host }: { api: OpsApi; host: AppHost }) {
  const [mode, setMode] = useState<Display>('UTC')
  const [week, setWeek] = useState(() => utcDate(new Date().toISOString()))
  const [day, setDay] = useState<string>()
  const [screen, setScreen] = useState<Screen>({ view: 'sked' })
  const back = useCallback(() => {
    setScreen((current) => current.view === 'prep'
      ? { view: 'card', selection: current.selection }
      : { view: 'sked' })
  }, [])
  const view = screen.view
  useEffect(() => {
    window.scrollTo(0, 0)
    if (view !== 'sked') return host.bindBack?.(back)
  }, [view, host, back])

  return (
    <main className="app-frame">
      <Header
        title={view === 'sked' ? 'SKED' : view === 'card' ? 'Flight Card' : 'Preparation'}
        back={view !== 'sked' && !host.bindBack ? back : undefined}
        mode={mode} toggleMode={() => setMode((value) => value === 'UTC' ? 'LT' : 'UTC')}
      />
      <div className="screen-content">
        {screen.view === 'sked' ? (
          <Schedule api={api} mode={mode} week={week} day={day} onWeek={setWeek} onDay={setDay}
            onFlight={(flight) => setScreen({ view: 'card', selection: { week, flight } })} />
        ) : screen.view === 'card' ? (
          <Card api={api} mode={mode} selection={screen.selection}
            onPrepare={() => setScreen({ view: 'prep', selection: screen.selection })} />
        ) : (
          <Preparation api={api} mode={mode} selection={screen.selection} openLink={host.openLink} />
        )}
      </div>
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
  api, selection, mode, onPrepare,
}: { api: OpsApi; selection: Selection; mode: Display; onPrepare: () => void }) {
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
