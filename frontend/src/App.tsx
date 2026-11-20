import { CalendarDays, ChevronLeft, Minus, PlaneTakeoff, Plus } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import type { Flight, OpsApi } from './api'
import { Badge, Button } from './components/ui'
import { Preparation } from './Preparation'
import { Resource, utcDate, utcTime } from './presentation'

export interface AppHost {
  bindBack?: (handler: () => void) => () => void
  openLink: (url: string) => void
}

const dateFormat = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'UTC', day: '2-digit', month: 'short', year: 'numeric',
})
const noData = '—'

function dayOffset(day: string, offset: number) {
  const date = new Date(day + 'T00:00:00Z')
  date.setUTCDate(date.getUTCDate() + offset)
  return utcDate(date.toISOString())
}

function Header({ title, back }: { title: string; back?: () => void }) {
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
      <Badge variant="accent">UTC / ICAO</Badge>
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

type Screen = { view: 'sked' } | { view: 'card' | 'prep'; flight: Flight }

export function App({ api, host }: { api: OpsApi; host: AppHost }) {
  const [day, setDay] = useState(() => utcDate(new Date().toISOString()))
  const [screen, setScreen] = useState<Screen>({ view: 'sked' })
  const back = useCallback(() => {
    setScreen((current) => current.view === 'prep'
      ? { view: 'card', flight: current.flight }
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
      />
      <div className="screen-content">
        {screen.view === 'sked' ? (
          <Schedule api={api} day={day} onDay={setDay} onFlight={(flight) => setScreen({ view: 'card', flight })} />
        ) : screen.view === 'card' ? (
          <Card api={api} day={day} flight={screen.flight} onPrepare={(flight) => setScreen({ view: 'prep', flight })} />
        ) : (
          <Preparation api={api} day={day} flight={screen.flight} openLink={host.openLink} />
        )}
      </div>
    </main>
  )
}

function Schedule({
  api, day, onDay, onFlight,
}: {
  api: OpsApi
  day: string
  onDay: (day: string) => void
  onFlight: (flight: Flight) => void
}) {
  const load = useCallback((signal: AbortSignal) => api.schedule(day, signal), [api, day])
  const today = utcDate(new Date().toISOString())
  return (
    <>
      <div className="date-bar">
        <Button variant="outline" size="icon" onClick={() => onDay(dayOffset(day, -1))} aria-label="Previous day">
          <Minus size={17} />
        </Button>
        <label className="date-control">
          <CalendarDays size={17} aria-hidden="true" />
          <span>{dateFormat.format(new Date(day + 'T00:00:00Z'))}</span>
          <input aria-label="Schedule date (UTC)" type="date" value={day}
            onChange={(event) => { if (/^\d{4}-\d{2}-\d{2}$/.test(event.target.value)) onDay(event.target.value) }} />
        </label>
        <Button variant="outline" size="icon" onClick={() => onDay(dayOffset(day, 1))} aria-label="Next day">
          <Plus size={17} />
        </Button>
        {today !== day && <Button variant="ghost" onClick={() => onDay(today)}>Today</Button>}
      </div>
      <Resource load={load} label="SKED">
        {(data) => (
          <section className="ops-panel" aria-label="Scheduled flights">
            <div className="panel-bar">
              <span>{data.flights.length} {data.flights.length === 1 ? 'leg' : 'legs'}</span>
              <Badge variant={data.complete ? 'default' : 'warning'}>{data.complete ? 'Leon' : 'Leon · Partial'}</Badge>
            </div>
            {data.flights.length === 0 ? (
              <p className="px-3 py-5 text-sm text-muted-foreground">
                {data.complete ? 'No flights scheduled.' : 'No flights in the partial schedule.'}
              </p>
            ) : (
              <>
                <div className="strip strip-head" aria-hidden="true"><span>STD</span><span>Flight</span><span>ADEP / ADES</span></div>
                <ul className="divide-y divide-border">
                  {data.flights.map((flight) => (
                    <li key={flight.id}>
                      <button className="strip strip-flight" onClick={() => onFlight(flight)}>
                        <time dateTime={flight.std} className="font-mono text-primary">{utcTime(flight.std)}</time>
                        <span className="flex min-w-0 flex-col items-start">
                          <strong className="wrap-anywhere">{flight.number ?? noData}</strong>
                          <span className="minor">{flight.aircraft ?? noData}</span>
                        </span>
                        <span className="flex min-w-0 flex-col items-start">
                          <strong className="font-mono">{flight.departure ?? noData} / {flight.arrival ?? noData}</strong>
                          {flight.cancelled
                            ? <Badge variant="warning">Cancelled</Badge>
                            : flight.ferry && <Badge>Ferry</Badge>}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </section>
        )}
      </Resource>
    </>
  )
}

function FlightLeg({ flight }: { flight: Flight }) {
  return (
    <section className="ops-panel">
      <div className="panel-bar">
        <span>{flight.number ?? noData} <span className="ml-1 text-muted-foreground">{flight.aircraft ?? noData}{flight.aircraft_type ? ' / ' + flight.aircraft_type : ''}</span></span>
        {flight.cancelled
          ? <Badge variant="warning">Cancelled</Badge>
          : <Badge>{dateFormat.format(new Date(flight.std))}</Badge>}
      </div>
      <dl className="grid grid-cols-2 divide-x divide-border">
        {([
          ['ADEP', flight.departure, 'STD', flight.std],
          ['ADES', flight.arrival, 'STA', flight.sta],
        ] as const).map(([station, airport, time, value]) => (
          <div key={station} className="space-y-1.5 p-3">
            <dt className="caption">{station}</dt>
            <dd className="font-mono text-2xl font-semibold">{airport ?? noData}</dd>
            <dt className="caption">{time}</dt>
            <dd className="font-mono text-sm text-primary">{value && utcDate(value) !== utcDate(flight.std) ? dateFormat.format(new Date(value)) + ' ' : ''}{utcTime(value)}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}

function Card({
  api, day, flight, onPrepare,
}: { api: OpsApi; day: string; flight: Flight; onPrepare: (flight: Flight) => void }) {
  const load = useCallback((signal: AbortSignal) => api.card(flight.id, day, signal), [api, day, flight.id])
  return (
    <Resource load={load} label="flight plan and handling" pending={<FlightLeg flight={flight} />}>
      {(card) => (
        <>
          <FlightLeg flight={card.flight} />
          <Button className="w-full" onClick={() => onPrepare(card.flight)}>Preparation</Button>
          <section className="ops-panel" aria-label="ForeFlight flight plan">
            <div className="panel-bar">
              <h2>Flight plan</h2><Badge variant={card.plan_status === 'partial' ? 'warning' : 'default'}>{card.plan_status === 'partial' ? 'ForeFlight · Partial' : 'ForeFlight'}</Badge>
            </div>
            {card.plan_status === 'unavailable' ? (
              <p className="px-3 py-3 text-sm text-muted-foreground">Plan unavailable.</p>
            ) : (
              <dl className="compact-fields">
                <dt>ETE</dt><dd>{card.ete_minutes === null ? noData : Math.floor(card.ete_minutes / 60) + 'h ' + String(card.ete_minutes % 60).padStart(2, '0') + 'm'}</dd>
                <dt>ALTN</dt><dd>{card.alternate ?? noData}</dd>
                <dt>RTE</dt><dd>{card.route ?? noData}</dd>
              </dl>
            )}
          </section>
          {card.handling.length > 0 && (
            <section className="ops-panel">
              <div className="panel-bar"><h2>Handling</h2><Badge>Leon</Badge></div>
              <dl className="compact-fields">
                {card.handling.map((handler) => (
                  <div className="contents" key={handler.station + handler.name}>
                    <dt>{handler.station}</dt>
                    <dd>
                      {handler.name}
                      {handler.phone && <span className="block text-muted-foreground">{handler.phone}</span>}
                      {handler.email && <span className="block text-muted-foreground">{handler.email}</span>}
                    </dd>
                  </div>
                ))}
              </dl>
            </section>
          )}
        </>
      )}
    </Resource>
  )
}
