import { CalendarDays, ChevronRight, Radar } from 'lucide-react'
import { useCallback } from 'react'
import type { OpsApi, Selection } from './api'
import { ToolRow } from './OpsCommon'
import { useResource, utcTime } from './presentation'
import { weekDay } from './Sked'
import type { LucideIcon } from 'lucide-react'

export type DashboardAction = 'ground' | 'sked' | 'brief' | 'docs' | 'analytics' | 'charter'

export function Dashboard({ api, today, selection, actions, onOpen, onCard }: {
  api: OpsApi; today: string; selection?: Selection
  actions: readonly { view: DashboardAction; label: string; icon: LucideIcon }[]
  onOpen: (view: DashboardAction) => void; onCard: (selection: Selection) => void
}) {
  const scheduleLoad = useCallback((signal: AbortSignal, refresh?: boolean) =>
    api.schedule(today, weekDay(today, 6), signal, refresh), [api, today])
  const { data: schedule, value: scheduleResult } = useResource(scheduleLoad)
  const { data: ground, value: groundResult } = useResource(api.ground)
  const { data: positions, value: fleetResult } = useResource(api.brief)
  const active = ground?.cards.filter((turn) => turn.status === 'active') ?? []
  const delayed = active.find((turn) => turn.blockers?.length)
  const turn = delayed ?? active[0]
  const upcoming = schedule?.flights.filter((flight) => !flight.cancelled) ?? []
  const next = upcoming.find((flight) => Date.parse(flight.std) >= Date.now())
  const readableGround = ground && ground.status !== 'UNAVAILABLE'
  const readableSchedule = schedule && schedule.status !== 'UNAVAILABLE'

  return <>
    <nav className="priority-actions" aria-label="Priority modules">
      <button className="priority-module priority-ground" onClick={() => onOpen('ground')}>
        <Radar size={23} aria-hidden="true" />
        <span className="priority-copy">
          <strong>GROUND STATUS</strong>
          <small>{turn ? [turn.station, turn.registration, delayed?.blockers?.[0].reason ?? turn.outbound_number].filter(Boolean).join(' · ')
            : readableGround ? 'No active turns' : groundResult ? 'Source unavailable' : 'Loading turns…'}</small>
        </span>
        <span className="priority-metric"><strong>{readableGround ? active.length + (ground.status === 'PARTIAL' ? '+' : '') : '—'}</strong><small>ACTIVE</small></span>
      </button>
      <button className="priority-module priority-sked" aria-label="SKED · Flight Schedule" onClick={() => onOpen('sked')}>
        <CalendarDays size={22} aria-hidden="true" />
        <span className="priority-copy">
          <strong>SKED · FLIGHT SCHEDULE</strong>
          <small>{next ? 'Next: ' + [next.number, [next.departure, next.arrival].filter(Boolean).join('→'), utcTime(next.std)].filter(Boolean).join(' · ')
            : readableSchedule ? 'No upcoming departures' : scheduleResult ? 'Source unavailable' : 'Loading schedule…'}</small>
        </span>
        <span className="priority-metric"><strong>{readableSchedule ? upcoming.length + (schedule.complete ? '' : '+') : '—'}</strong><small>7 DAYS</small></span>
      </button>
    </nav>
    <section className="dashboard-secondary">
      <h2 className="section-kicker">Fleet &amp; Commercial</h2>
      <nav className="quick-actions" aria-label="Fleet and commercial modules">
        {actions.slice(2).map(({ view, label, icon: Icon }) => <button className="utility-module" key={view} onClick={() => onOpen(view)}>
          <Icon size={17} aria-hidden="true" />{label}<ChevronRight className="ml-auto opacity-50" size={14} aria-hidden="true" />
        </button>)}
      </nav>
    </section>
    <section className="ops-panel">
      <button className="panel-bar w-full text-left" onClick={() => onOpen('brief')}>
        <h2 className="section-kicker">Fleet Positions</h2><span className="minor flex items-center gap-1">LEON <ChevronRight size={13} /></span>
      </button>
      {positions?.fleet?.length ? positions.fleet.slice(0, 5).map((item) => {
        const liveTurn = active.find((row) => row.registration === item.registration && (!item.station || row.station === item.station))
        const blocker = liveTurn?.blockers?.length
        const ready = !item.active && liveTurn?.ready_at
        const status = blocker ? 'BLOCKER' : ready ? 'READY' : item.active ? 'IN FLIGHT WINDOW' : item.next
          ? (item.next.ferry ? 'FERRY ' : 'STD ') + utcTime(item.next.std)
          : item.station ? 'LAST ARRIVAL' : 'POSITION UNKNOWN'
        return <div className="fleet-position" key={item.registration}>
          <span>{item.registration}{item.station && ' @ ' + item.station}</span>
          <span className={blocker ? 'text-warning' : ready ? 'text-primary' : 'text-muted-foreground'}>{status}</span>
        </div>
      }) : <p className="tool-line text-muted-foreground">{positions?.status === 'READY' ? 'No fleet positions in the schedule window' : fleetResult ? 'Fleet positions unavailable' : 'Loading fleet positions…'}</p>}
    </section>
    {selection && <div className="ops-panel">
      <ToolRow title={selection.flight.number ?? 'Selected flight'} detail={selection.flight.aircraft ?? undefined} action="FLIGHT CARD" onClick={() => onCard(selection)} />
    </div>}
  </>
}
