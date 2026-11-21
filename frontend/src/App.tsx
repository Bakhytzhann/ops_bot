import { CalendarDays, ChartNoAxesCombined, ChevronLeft, ClipboardList, FolderOpen, PlaneTakeoff, Radar, Search } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import type { Display, OpsApi, Selection } from './api'
import { Badge, Button } from './components/ui'
import { Dashboard, type DashboardAction } from './Dashboard'
import { FleetDocs, OpsBrief } from './FleetTools'
import { FlightCard } from './FlightCard'
import { GroundStation, GroundStatus, GroundTurn } from './GroundStatus'
import { turnForFlight, type LegRef, type Turn } from './ops-types'
import { Preparation } from './Preparation'
import { Analytics, CharterCheck, CharterRequest, CharterSales } from './ReportingTools'
import { Schedule, type SkedFilters, weekDay } from './Sked'
import { Resource, utcDate, useUtcClock } from './presentation'

export interface AppHost {
  bindBack?: (handler: () => void) => () => void
  openLink: (url: string) => void
}

function Header({ title, back, mode, toggleMode, clock, subtitle }: {
  title: string; back?: () => void; mode?: Display; toggleMode?: () => void
  clock?: string; subtitle?: string
}) {
  const [online, setOnline] = useState(navigator.onLine)
  useEffect(() => {
    const update = () => setOnline(navigator.onLine)
    window.addEventListener('online', update)
    window.addEventListener('offline', update)
    return () => {
      window.removeEventListener('online', update)
      window.removeEventListener('offline', update)
    }
  }, [])
  return (
    <header className="screen-header">
      <div className="flex min-w-0 items-center gap-2.5">
        {back ? <Button variant="outline" size="icon" onClick={back} aria-label="Back"><ChevronLeft size={19} /></Button>
          : <span className="brand-mark"><PlaneTakeoff size={19} aria-hidden="true" /></span>}
        <div className="min-w-0">
          {subtitle && <p className="header-subtitle">{subtitle}</p>}
          <h1 className="header-title">{title}</h1>
        </div>
      </div>
      {toggleMode && <Button variant="outline" onClick={toggleMode} className="mode-toggle" aria-label="Switch time and airport code display">
        {mode === 'LT' ? 'LT · IATA' : 'UTC · ICAO'}
      </Button>}
      {clock && <div className="cockpit-clock">
        <time dateTime={clock}>{clock.slice(11, 16)} UTC</time>
        <Badge variant={online ? 'accent' : 'warning'}>{online ? 'ONLINE' : 'OFFLINE'}</Badge>
      </div>}
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
  { view: 'ground', label: 'GROUND STATUS', icon: Radar },
  { view: 'sked', label: 'SKED · SCHEDULE', icon: CalendarDays },
  { view: 'brief', label: 'OPS BRIEF', icon: ClipboardList },
  { view: 'docs', label: 'FLEET DOCS', icon: FolderOpen },
  { view: 'analytics', label: 'ANALYTICS', icon: ChartNoAxesCombined },
  { view: 'charter', label: 'CHARTER', icon: Search },
] as const

type Screen =
  | { view: 'dashboard' }
  | { view: Exclude<DashboardAction, 'docs'> }
  | { view: 'docs'; checklist?: boolean }
  | { view: 'card' | 'prep'; selection: Selection }
  | { view: 'linked'; leg: LegRef }
  | { view: 'ground-linked'; selection: Selection }
  | { view: 'ground-turn'; turn: Turn }
  | { view: 'ground-station'; station: string }
  | { view: 'charter-sales' }
  | { view: 'charter-request'; id: number }

const screenTitles: Partial<Record<Screen['view'], string>> = {
  dashboard: 'OPS · DISPATCH EFB', card: 'FLIGHT CARD', prep: 'PREPARATION', linked: 'FLIGHT CARD',
  'ground-linked': 'GROUND STATUS', 'ground-turn': 'GROUND TURN', 'ground-station': 'STATION STATISTICS',
  'charter-sales': 'Charter Sales', 'charter-request': 'Quote Request',
}

function ResolvedGround({ turn, onSelect }: { turn?: Turn; onSelect: (turn?: Turn) => void }) {
  useEffect(() => onSelect(turn), [onSelect, turn])
  return null
}

function ResolvedCard({ selection, onSelect }: { selection: Selection; onSelect: (selection: Selection) => void }) {
  useEffect(() => onSelect(selection), [onSelect, selection])
  return null
}

function LinkedFlight({ api, leg, week, onSelect, onSked }: {
  api: OpsApi; leg: LegRef; week: string; onSelect: (selection: Selection) => void; onSked: () => void
}) {
  const day = leg.std?.slice(0, 10) ?? ''
  const first = day >= week && day <= weekDay(week, 6) ? week : day
  const load = useCallback((signal: AbortSignal, refresh?: boolean) => api.schedule(first, weekDay(first, 6), signal, refresh), [api, first])
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
  const clock = useUtcClock()
  const [filters, setFilters] = useState<SkedFilters>({ tail: '', query: '', hideCancelled: false })
  const [mode, setMode] = useState<Display>('UTC')
  const [week, setWeek] = useState(() => utcDate(new Date().toISOString()))
  const [day, setDay] = useState<string>()
  const [selection, setSelection] = useState<Selection>()
  const [stack, setStack] = useState<Screen[]>([{ view: 'dashboard' }])
  const screen = stack[stack.length - 1]
  const back = useCallback(() => {
    const sheet = document.querySelector<HTMLDialogElement>('dialog[open]')
    if (sheet) sheet.close()
    else setStack((current) => current.length > 1 ? current.slice(0, -1) : current)
  }, [])
  const open = useCallback((next: Screen) => setStack((current) => [...current, next]), [])
  const openSked = useCallback(() => open({ view: 'sked' }), [open])
  const openCard = useCallback((picked: Selection) => {
    setSelection(picked)
    open({ view: 'card', selection: picked })
  }, [open])
  const forGround = useCallback((picked: Selection, turn?: Turn) => {
    setSelection(picked)
    open(turn ? { view: 'ground-turn', turn } : { view: 'ground-linked', selection: picked })
  }, [open])
  const openLinked = useCallback((leg: LegRef) => {
    if (leg.std && leg.id) open({ view: 'linked', leg })
    else openSked()
  }, [open, openSked])
  const fromLinked = useCallback((picked: Selection) => {
    setSelection(picked)
    setStack((current) => current.at(-1)?.view === 'linked' ? [...current.slice(0, -1), { view: 'card', selection: picked }] : current)
  }, [])
  const fromGroundLink = useCallback((turn?: Turn) => {
    setStack((current) =>
      current.at(-1)?.view === 'ground-linked' ? [...current.slice(0, -1), turn ? { view: 'ground-turn', turn } : { view: 'ground' }] : current)
  }, [])
  const view = screen.view
  useEffect(() => {
    window.scrollTo(0, 0)
    if (view !== 'dashboard') return host.bindBack?.(back)
  }, [screen, view, host, back])

  let content
  switch (screen.view) {
    case 'dashboard':
      content = <Dashboard api={api} today={utcDate(clock)} selection={selection} actions={actions}
        onOpen={(action) => open({ view: action })} onCard={openCard} />
      break
    case 'sked':
      content = <Schedule api={api} mode={mode} week={week} day={day} onWeek={setWeek} onDay={setDay}
        filters={filters} onFilters={setFilters} onFlight={(flight) => openCard({ week, flight })}
        onGround={(flight, turn) => forGround({ week, flight }, turn)} />
      break
    case 'card':
      content = <FlightCard api={api} mode={mode} selection={screen.selection}
        onPrepare={() => open({ view: 'prep', selection: screen.selection })}
        onGround={(turn) => forGround(screen.selection, turn)}
        onChecklist={() => { setSelection(screen.selection); open({ view: 'docs', checklist: true }) }} />
      break
    case 'prep':
      content = <Preparation api={api} mode={mode} selection={screen.selection} openLink={host.openLink} />
      break
    case 'linked':
      content = <LinkedFlight api={api} leg={screen.leg} week={week} onSelect={fromLinked} onSked={openSked} />
      break
    case 'ground-linked':
      content = <Resource load={api.ground} label="selected ground turn">
        {(data) => <ResolvedGround turn={turnForFlight(data.cards, screen.selection.flight.id)} onSelect={fromGroundLink} />}
      </Resource>
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
        back={view !== 'dashboard' ? back : undefined} clock={view === 'dashboard' || view === 'ground-turn' ? clock : undefined}
        subtitle={screen.view === 'card' || screen.view === 'prep'
          ? [screen.selection.flight.number, screen.selection.flight.aircraft].filter(Boolean).join(' · ')
          : screen.view === 'ground-turn'
            ? [screen.turn.station, screen.turn.registration].filter(Boolean).join(' · ')
            : undefined}
        mode={mode} toggleMode={chooseMode ? () => setMode((value) => value === 'UTC' ? 'LT' : 'UTC') : undefined} />
      <div className="screen-content">{content}</div>
    </main>
  )
}
