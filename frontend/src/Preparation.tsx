import { ExternalLink, Plane } from 'lucide-react'
import { useCallback, useState } from 'react'
import type { CrewMember, CrewPreparation, Display, Load, NotamPreparation, OpsApi, Selection, WeatherPreparation, WeatherReport } from './api'
import { Badge, Button, Tabs, TabsContent, TabsList, TabsTrigger } from './components/ui'
import { CopyButton, flightAirport, flightTime, PanelHeader, Resource, StatusBadge, utcStamp, utcTime } from './presentation'

const sections = [
  { id: 'crew', title: 'CREW / FDP' },
  { id: 'weather', title: 'WX / TAF' },
  { id: 'notams', title: 'NOTAM' },
] as const
const signedMinutes = new Intl.NumberFormat('en', { signDisplay: 'exceptZero', maximumFractionDigits: 1 })
const pilots = ['CPT', 'FO'] as const
const unknown = 'NOT VERIFIED'
type Section = typeof sections[number]['id']

export function Preparation({
  api, selection, mode, openLink,
}: { api: OpsApi; selection: Selection; mode: Display; openLink: (url: string) => void }) {
  const [section, setSection] = useState<Section>('crew')
  const loadCrew: Load<CrewPreparation> = useCallback(
    (signal, refresh) => api.crew(selection, signal, refresh), [api, selection],
  )
  const loadWeather: Load<WeatherPreparation> = useCallback(
    (signal, refresh) => api.weather(selection, signal, refresh), [api, selection],
  )
  const loadNotams: Load<NotamPreparation> = useCallback(
    (signal, refresh) => api.notams(selection, signal, refresh), [api, selection],
  )
  const { flight } = selection
  return (
    <>
      <div className="prep-leg">
        <div className="flex items-center gap-2 font-mono font-semibold">
          <span>{flightAirport(flight, 'departure', mode)}</span><Plane size={15} className="text-primary" aria-hidden="true" />
          <span>{flightAirport(flight, 'arrival', mode)}</span>
        </div>
        <span className="minor">STD {flightTime(flight, 'std', mode)} · STA {flightTime(flight, 'sta', mode)}</span>
      </div>
      <Tabs value={section} onValueChange={(value) => setSection(value as Section)}>
        <TabsList aria-label="Preparation sections">
          {sections.map(({ id, title }) => <TabsTrigger key={id} value={id}>{title}</TabsTrigger>)}
        </TabsList>
        <TabsContent value="crew">
          {section === 'crew' && <Resource load={loadCrew} label="CREW / FDP">
            {(data, refresh, refreshing) => <><CrewPanel data={data} detailed /><SectionRefresh onClick={refresh} disabled={refreshing} /></>}
          </Resource>}
        </TabsContent>
        <TabsContent value="weather">
          {section === 'weather' && <Resource load={loadWeather} label="WX / TAF">
            {(data, refresh, refreshing) => <><WeatherPanel data={data} selection={selection} mode={mode} detailed /><SectionRefresh onClick={refresh} disabled={refreshing} /></>}
          </Resource>}
        </TabsContent>
        <TabsContent value="notams">
          {section === 'notams' && <Resource load={loadNotams} label="NOTAM">
            {(data, refresh, refreshing) => <><Notams data={data} openLink={openLink} /><SectionRefresh onClick={refresh} disabled={refreshing} /></>}
          </Resource>}
        </TabsContent>
      </Tabs>
    </>
  )
}

function SectionRefresh({ onClick, disabled }: { onClick: () => void; disabled: boolean }) {
  return <Button className="mt-2 w-full" variant="outline" onClick={onClick} disabled={disabled}>Refresh section</Button>
}

function Field({ name, value }: { name: string; value: string | null }) {
  return <div className="contents"><dt>{name}</dt><dd>{value ?? unknown}</dd></div>
}

function durationMinutes(value?: string | null) {
  const clock = value?.match(/^(\d+):([0-5]\d)(?::([0-5]\d))?$/)
  const units = value?.match(/^(\d+)\s*h\s*(\d+)\s*m$/i)
  const parts = clock ?? units
  return parts ? Number(parts[1]) * 60 + Number(parts[2]) + Number(parts[3] ?? 0) / 60 : null
}

function DutyBar({ fdp }: { fdp?: NonNullable<CrewMember['duty']>['metrics'][number] }) {
  const actual = durationMinutes(fdp?.actual)
  const limit = durationMinutes(fdp?.limit)
  if (actual === null || limit === null || limit <= 0) return null
  return <div className="duty-gauge" data-offence={fdp?.offence || actual > limit}>
    <div><span>FDP</span><strong>{fdp?.actual} / {fdp?.limit}</strong></div>
    <progress value={Math.min(actual, limit)} max={limit} aria-label="Flight duty period versus Leon limit" />
  </div>
}

function CrewReadout({ member, detailed }: { member: CrewMember; detailed: boolean }) {
  const duty = member.duty
  const fdp = duty?.metrics.find((metric) => metric.name === 'FDP')
  const restBefore = duty?.metrics.find((metric) => metric.name === 'Rest Before')
  const restAfter = duty?.metrics.find((metric) => metric.name === 'Rest After')
  return (
    <>
      <strong>{member.name ?? unknown}</strong>
      {member.position && member.position !== member.role && <span className="ml-2 minor">{member.position}</span>}
      {detailed && <DutyBar fdp={fdp} />}
      <dl className="compact-fields metrics-detail mt-2">
        <Field name="REPORT ON" value={duty?.reporting ? utcStamp(duty.start) : null} />
        <Field name="DUTY OFF" value={duty?.reporting ? utcStamp(duty.end) : null} />
        {detailed && duty && !duty.reporting && duty.start && duty.end && (
          <Field name="ROSTER" value={utcStamp(duty.start) + ' / ' + utcStamp(duty.end)} />
        )}
        <Field name="DUTY DURATION" value={duty?.duty_duration ?? null} />
        {!detailed && <Field name="FDP ACT / LIMIT" value={fdp ? (fdp.actual ?? '—') + ' / ' + (fdp.limit ?? '—') : null} />}
        <Field name="FDP REMAINING" value={duty?.fdp_remaining ?? null} />
        {!detailed && <Field name="REST BEFORE" value={restBefore?.actual ?? null} />}
        {!detailed && <Field name="REST AFTER" value={restAfter?.actual ?? null} />}
        <Field name="NEXT EOBT" value={utcStamp(duty?.next_eobt ?? null)} />
        {detailed && duty?.metrics.map((metric) => (
          <Field key={metric.name} name={metric.name + (metric.limit_type ? ' · ' + metric.limit_type : '')}
            value={(metric.actual ?? '—') + ' / ' + (metric.limit ?? '—') + (metric.offence ? ' · OFFENCE' : '')} />
        ))}
      </dl>
      {!!duty?.violations.length && (
        <p className="mt-2 text-xs text-warning">Leon offences · {duty.violations.join(' / ')}</p>
      )}
    </>
  )
}

export function CrewPanel({
  data, detailed = false, assignments = false,
}: { data: CrewPreparation; detailed?: boolean; assignments?: boolean }) {
  return (
    <section className="ops-panel" aria-label="Crew and FDP">
      <PanelHeader title={detailed ? 'FTL & REST AUDIT' : 'CREW / FDP'} source="Leon" state={assignments ? { ...data, status: data.available ? 'PARTIAL' : 'UNAVAILABLE' } : data} />
      <div className="divide-y divide-border">
        {pilots.map((role) => {
          const members = data.members.filter((member) => member.role === role)
          return <div className="p-3 text-sm" key={role}>
            <Badge variant="accent" className="mb-1">{role}</Badge>
            {members.length
              ? members.map((member, index) => <div key={index} className="mb-2 last:mb-0">
                {assignments ? <strong>{member.name ?? unknown}</strong> : <CrewReadout member={member} detailed={detailed} />}
              </div>)
              : <p className="text-muted-foreground">{unknown}</p>}
          </div>
        })}
      </div>
    </section>
  )
}

export function HotelPanel({ data }: { data: CrewPreparation }) {
  const verified = pilots.map((role) => {
    const assigned = data.members.filter((member) => member.role === role)
    return assigned.length === 1 ? assigned[0].hotel : null
  })
  const status = verified.every(Boolean) ? 'READY' : verified.some(Boolean) ? 'PARTIAL' : 'UNAVAILABLE'
  return (
    <section className="ops-panel" aria-label="Hotel">
      <PanelHeader title="HOTEL" source="Leon" state={{ ...data, status }} />
      <dl className="compact-fields">
        {pilots.map((role, index) => <Field key={role} name={role} value={verified[index]} />)}
      </dl>
    </section>
  )
}

function Report({ report, full, flightTimeUtc }: { report: WeatherReport; full: boolean; flightTimeUtc?: string | null }) {
  const window = flightTimeUtc && report.product === 'TAF' ? Date.parse(flightTimeUtc) : NaN
  const covers = report.valid_from && report.valid_to && Date.parse(report.valid_from) <= window && window < Date.parse(report.valid_to)
  return (
    <div className="mt-2 space-y-1.5 border-t border-border pt-2">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <strong className="font-mono text-xs">{report.product}</strong>
        <span className="minor">{report.sources.join(' / ')} · {utcStamp(report.issued_at)}</span>
        <StatusBadge status={report.status} until={report.fresh_until} />
      </div>
      {full ? (
        <>
          {report.product === 'TAF' && (
            <p className="minor">VALID {utcStamp(report.valid_from)} / {utcStamp(report.valid_to)}</p>
          )}
          <pre className="raw-text weather-raw" data-window={!!covers}>{report.raw}</pre>
          {covers && <p className="minor text-warning">TAF covers scheduled {utcTime(flightTimeUtc)}</p>}
        </>
      ) : <p className="text-sm">{Object.entries(report.facts).map(([key, value]) => key + ' ' + value).join(' · ') || unknown}</p>}
    </div>
  )
}

export function WeatherPanel({
  data, selection, mode, detailed = false,
}: { data: WeatherPreparation; selection: Selection; mode: Display; detailed?: boolean }) {
  const { flight } = selection
  const stations = [
    { station: flight.departure, label: 'DEP ' + flightAirport(flight, 'departure', mode), stamp: flight.std },
    { station: flight.arrival, label: 'ARR ' + flightAirport(flight, 'arrival', mode), stamp: flight.sta },
    ...(detailed ? data.stations.filter((station) => station !== flight.departure && station !== flight.arrival)
      .map((station) => ({ station, label: 'ALTN ' + station, stamp: null })) : []),
  ]
  const raw = [...new Set(data.reports.map((item) => item.raw))].join('\n\n')
  return (
    <section className="ops-panel">
      <PanelHeader title={detailed ? 'FULL METAR / TAF' : 'WX · CURRENT METAR'} source="Leon / AWC" state={data}
        action={detailed && raw && <CopyButton text={raw} label="Copy WX" />} />
      {stations.map(({ station, label, stamp }) => {
        const products = detailed ? ['METAR', 'TAF'] as const : ['METAR'] as const
        return (
          <div className="border-b border-border p-3 last:border-b-0" key={label}>
            <h3 className="font-mono text-sm font-semibold">{label}</h3>
            {products.map((product) => {
              const reports = data.reports.filter((report) => report.station === station && report.product === product)
              if (!reports.length) return <p className="mt-1 text-sm text-muted-foreground" key={product}>{product} · UNAVAILABLE</p>
              return (
                <div key={product}>
                  {(detailed ? reports : reports.slice(0, 1)).map((report) => (
                    <Report key={report.raw} report={report} full={detailed} flightTimeUtc={stamp} />
                  ))}
                  {!detailed && reports.length > 1 && <p className="mt-1 minor">Multiple reports in Preparation.</p>}
                </div>
              )
            })}
          </div>
        )
      })}
      {!detailed && (
        <details className="notam-detail m-2.5">
          <summary>CAT I · Reference</summary>
          <p className="px-2.5 pb-2.5 text-xs text-muted-foreground">
            Category definition only. DH ≥ 200 ft and either RVR ≥ 550 m or VIS ≥ 800 m.
            Use the published approach and operator minima.
          </p>
        </details>
      )}
    </section>
  )
}

function Notams({ data, openLink }: { data: NotamPreparation; openLink: (url: string) => void }) {
  const briefingUrl = data.briefing_url
  const [filter, setFilter] = useState<'all' | 'affects' | 'review'>('all')
  const [selectedStation, setSelectedStation] = useState<string>()
  const stations = data.stations.filter((station) => !selectedStation || station === selectedStation)
  return (
    <section className="ops-panel">
      <PanelHeader title="NOTAM" source="ForeFlight briefing" state={data} />
      <div className="space-y-2 border-b border-border p-3 text-sm text-muted-foreground">
        <p>Briefing · {utcStamp(data.briefing_time)}</p>
        {!!data.departure_offset_minutes && (
          <p className="text-warning">Departure differs from Leon by {signedMinutes.format(data.departure_offset_minutes)} min.</p>
        )}
        {briefingUrl ? (
          <Button variant="outline" className="w-full" onClick={() => openLink(briefingUrl)}>
            <ExternalLink size={15} /> Open briefing
          </Button>
        ) : <p>Briefing · UNAVAILABLE</p>}
      </div>
      <div className="notam-filters">
        <div className="filter-belt" role="group" aria-label="NOTAM severity">
          {(['all', 'affects', 'review'] as const).map((status) =>
            <button key={status} className="filter-chip" aria-pressed={filter === status} onClick={() => setFilter(status)}>
              {status.toUpperCase()} · {data.notams.filter((record) => status === 'all' || record.status === status).length}
            </button>)}
        </div>
        <div className="filter-belt" role="group" aria-label="NOTAM station">
          <button className="filter-chip" aria-pressed={!selectedStation} onClick={() => setSelectedStation(undefined)}>All stations</button>
          {data.stations.map((station) => <button key={station} className="filter-chip"
            aria-pressed={station === selectedStation} onClick={() => setSelectedStation(station)}>{station}</button>)}
        </div>
      </div>
      {stations.map((station) => {
        const records = data.notams.filter((record) => record.station === station && (filter === 'all' || record.status === filter))
        return (
          <div className="border-b border-border p-3 last:border-b-0" key={station}>
            <h3 className="mb-2 font-mono text-sm font-semibold">{station}</h3>
            {!data.recognized.includes(station) && <p className="text-sm text-muted-foreground">{unknown}</p>}
            {data.recognized.includes(station) && !records.length && (
              <p className="text-sm text-muted-foreground">{filter === 'all' ? 'No applicable or review items in the briefing.' : 'No items match this filter.'}</p>
            )}
            {records.map((record, index) => (
              <details className="notam-detail" key={record.id + index}>
                <summary><span>{record.id}</span><Badge variant={record.status === 'affects' ? 'warning' : 'default'}>{record.status}</Badge></summary>
                <pre className="raw-text">{record.raw}</pre>
              </details>
            ))}
          </div>
        )
      })}
    </section>
  )
}
