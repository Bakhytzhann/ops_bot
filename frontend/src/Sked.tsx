import { CalendarDays, ChevronLeft, ChevronRight, Radar, RefreshCw, Search } from 'lucide-react'
import { useCallback, useState } from 'react'
import { Badge, Button } from './components/ui'
import type { Display, Flight, OpsApi } from './api'
import { turnForFlight, type Turn } from './ops-types'
import { flightAirport, flightTime, ResourceView, StatusBadge, useResource, utcDate, utcTime } from './presentation'
import { Sheet } from './Sheet'

export type SkedFilters = { tail: string; query: string; hideCancelled: boolean }

const dayFormat = new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', weekday: 'short', day: '2-digit', month: 'short' })

export function weekDay(day: string, offset: number) {
  const date = new Date(day + 'T00:00:00Z')
  date.setUTCDate(date.getUTCDate() + offset)
  return utcDate(date.toISOString())
}

function dayLabel(day: string) {
  return dayFormat.format(new Date(day + 'T00:00:00Z'))
}

function FlightBadges({ flight, blockers, complete }: { flight: Flight; blockers: number; complete: boolean }) {
  return <span className="flex flex-wrap items-center gap-1">
    {flight.cancelled ? <Badge className="line-through">CNL</Badge> : <>
      {blockers > 0 ? <Badge variant="warning">{blockers} {blockers === 1 ? 'BLOCKER' : 'BLOCKERS'}</Badge>
        : <Badge variant={complete ? 'default' : 'warning'}>{complete ? 'PLANNED' : 'PARTIAL SKED'}</Badge>}
      {flight.ferry && <Badge>FERRY</Badge>}
    </>}
  </span>
}

export function Schedule({
  api, mode, week, day, onWeek, onDay, onFlight, onGround, filters, onFilters,
}: {
  api: OpsApi; mode: Display; week: string; day?: string
  onWeek: (day: string) => void; onDay: (day?: string) => void
  onFlight: (flight: Flight) => void; onGround: (flight: Flight, turn?: Turn) => void
  filters: SkedFilters; onFilters: (filters: SkedFilters) => void
}) {
  const [focused, setFocused] = useState<Flight>()
  const end = weekDay(week, 6)
  const load = useCallback((signal: AbortSignal, refresh?: boolean) => api.schedule(week, end, signal, refresh), [api, week, end])
  const schedule = useResource(load)
  const { data: ground } = useResource(api.ground)
  const blockerCount = (flight: Flight) => ground?.cards.find((turn) => turn.status === 'active' && turn.outbound_id === flight.id)?.blockers?.length ?? 0
  const days = Array.from({ length: 7 }, (_, index) => weekDay(week, index))
  const terms = filters.query.trim().toUpperCase().split(/[\s/→]+/).filter(Boolean)

  function changeWeek(value: string) {
    onWeek(value)
    onDay()
  }

  return <>
    <section className="ops-panel schedule-controls">
      <div className="week-controls">
        <Button variant="outline" size="icon" aria-label="Previous seven days" onClick={() => changeWeek(weekDay(week, -7))}><ChevronLeft size={16} /></Button>
        <label className="date-control">
          <CalendarDays size={14} aria-hidden="true" />
          <span>{week.slice(5)} / {end.slice(5)} UTC</span>
          <input aria-label="Schedule date (UTC)" type="date" value={day ?? week} onChange={(event) => {
            const value = event.target.value
            if (!value) return
            if (value < week || value > end) onWeek(value)
            onDay(value)
          }} />
        </label>
        <Button variant="outline" size="icon" aria-label="Next seven days" onClick={() => changeWeek(weekDay(week, 7))}><ChevronRight size={16} /></Button>
        <Button variant="outline" size="icon" aria-label="Refresh SKED" disabled={schedule.refreshing} onClick={schedule.refresh}><RefreshCw size={15} /></Button>
      </div>
      <div className="filter-belt" role="group" aria-label="Schedule days">
        <button className="filter-chip" aria-pressed={!day} onClick={() => onDay()}>All 7 days</button>
        {days.map((value) => <button className="filter-chip" key={value} aria-pressed={value === day} onClick={() => onDay(value)}>{dayLabel(value)}</button>)}
      </div>
    </section>
    <ResourceView result={schedule} label="SKED">
    {(data) => {
      const tails = [...new Set([filters.tail, ...data.flights.map((flight) => flight.aircraft ?? '')].filter(Boolean))].sort()
      const visible = data.flights.filter((flight) => {
        if (day && flight.std.slice(0, 10) !== day) return false
        if (filters.tail && filters.tail !== flight.aircraft) return false
        if (filters.hideCancelled && flight.cancelled) return false
        const searchable = [flight.number, flight.aircraft, flight.departure, flight.arrival, flight.departure_iata, flight.arrival_iata].filter(Boolean).join(' ').toUpperCase()
        return terms.every((term) => searchable.includes(term))
      })
      return <>
        <section className="ops-panel schedule-controls">
          <div className="filter-belt" role="group" aria-label="Aircraft registrations">
            <button className="filter-chip" aria-pressed={!filters.tail} onClick={() => onFilters({ ...filters, tail: '' })}>All tails</button>
            {tails.map((tail) => <button className="filter-chip" key={tail} aria-pressed={tail === filters.tail} onClick={() => onFilters({ ...filters, tail })}>{tail}</button>)}
          </div>
          <div className="schedule-search">
            <label className="search-field"><Search size={15} aria-hidden="true" />
              <input type="search" aria-label="Search flights" placeholder="Flight, ICAO / IATA, tail" value={filters.query}
                onChange={(event) => onFilters({ ...filters, query: event.target.value })} />
            </label>
            <button className="filter-chip" aria-pressed={filters.hideCancelled}
              onClick={() => onFilters({ ...filters, hideCancelled: !filters.hideCancelled })}>Hide CNL</button>
          </div>
        </section>
        <section className="ops-panel" aria-label="Scheduled flights">
          <header className="panel-bar">
            <div><h2>{visible.length} / {data.flights.length} legs</h2><p className="minor">Leon{data.fetched_at && ' · ' + utcTime(data.fetched_at)}</p></div>
            <StatusBadge status={data.status} until={data.fresh_until} />
          </header>
          {visible.length ? <ul>
            {visible.map((flight, index) => {
              const legDay = flight.std.slice(0, 10)
              return <li key={flight.id}>
                {(index === 0 || visible[index - 1].std.slice(0, 10) !== legDay) && <h3 className="schedule-day">{dayLabel(legDay)} UTC</h3>}
                <button className="schedule-leg" data-cancelled={flight.cancelled} onClick={() => setFocused(flight)}>
                  <span className="schedule-times"><strong>{flightTime(flight, 'std', mode)}</strong><small>{flightTime(flight, 'sta', mode)}</small></span>
                  <span className="min-w-0">
                    <strong className="block font-mono wrap-anywhere">{flightAirport(flight, 'departure', mode)} → {flightAirport(flight, 'arrival', mode)}</strong>
                    <small className="minor">{[flight.number, flight.aircraft, flight.aircraft_type].filter(Boolean).join(' · ') || 'Unassigned'}</small>
                  </span>
                  <span className="schedule-badges"><FlightBadges flight={flight} blockers={blockerCount(flight)} complete={data.complete} /><ChevronRight size={14} aria-hidden="true" /></span>
                </button>
              </li>
            })}
          </ul> : <p className="tool-line text-muted-foreground">{data.flights.length ? 'No flights match these filters.' : data.complete ? 'No flights scheduled.' : 'No flights in the partial schedule.'}</p>}
        </section>
        {focused && <Sheet title={focused.number ?? 'Flight'} onClose={() => setFocused(undefined)}>
          <div className="sheet-content">
            <h3 className="font-mono text-xl font-bold">{flightAirport(focused, 'departure', mode)} → {flightAirport(focused, 'arrival', mode)}</h3>
            <p className="minor">{[focused.aircraft, focused.aircraft_type, flightTime(focused, 'std', mode), flightTime(focused, 'sta', mode)].filter(Boolean).join(' · ')}</p>
            <FlightBadges flight={focused} blockers={blockerCount(focused)} complete={data.complete} />
            <div className="grid grid-cols-2 gap-2">
              <Button onClick={() => { setFocused(undefined); onFlight(focused) }}>Open Flight Card</Button>
              <Button variant="outline" onClick={() => { setFocused(undefined); onGround(focused, turnForFlight(ground?.cards, focused.id)) }}><Radar size={15} /> Ground Turn</Button>
            </div>
          </div>
        </Sheet>}
      </>
    }}
    </ResourceView>
  </>
}
