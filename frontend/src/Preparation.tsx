import { ExternalLink } from 'lucide-react'
import { useCallback, useState } from 'react'
import type { CrewMember, CrewPreparation, Display, Load, NotamPreparation, OpsApi, Selection, WeatherPreparation, WeatherReport } from './api'
import { Badge, Button, Tabs, TabsContent, TabsList, TabsTrigger } from './components/ui'
import { flightAirport, flightTime, PanelHeader, Resource, StatusBadge, utcStamp } from './presentation'

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
        <strong>{flight.number ?? unknown}</strong>
        <span className="font-mono">{flightAirport(flight, 'departure', mode)} / {flightAirport(flight, 'arrival', mode)}</span>
        <span className="font-mono text-muted-foreground">{flightTime(flight, 'std', mode)}</span>
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

function CrewReadout({ member, detailed }: { member: CrewMember; detailed: boolean }) {
  const duty = member.duty
  const fdp = duty?.metrics.find((metric) => metric.name === 'FDP')
  const restBefore = duty?.metrics.find((metric) => metric.name === 'Rest Before')
  const restAfter = duty?.metrics.find((metric) => metric.name === 'Rest After')
  return (
    <>
      <strong>{member.name ?? unknown}</strong>
      {member.position && member.position !== member.role && <span className="ml-2 minor">{member.position}</span>}
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
      <PanelHeader title="CREW / FDP" source="Leon" state={assignments ? { ...data, status: data.available ? 'PARTIAL' : 'UNAVAILABLE' } : data} />
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

function Report({ report, full }: { report: WeatherReport; full: boolean }) {
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
          <pre className="raw-text">{report.raw}</pre>
        </>
      ) : <p className="text-sm">{Object.entries(report.facts).map(([key, value]) => key + ' ' + value).join(' · ') || unknown}</p>}
    </div>
  )
}

export function WeatherPanel({
  data, selection, mode, detailed = false,
}: { data: WeatherPreparation; selection: Selection; mode: Display; detailed?: boolean }) {
  return (
    <section className="ops-panel">
      <PanelHeader title={detailed ? 'WX / TAF' : 'WX · METAR'} source="Leon / AWC" state={data} />
      {(['departure', 'arrival'] as const).map((side) => {
        const station = selection.flight[side]
        const products = detailed ? ['METAR', 'TAF'] as const : ['METAR'] as const
        return (
          <div className="border-b border-border p-3 last:border-b-0" key={side}>
            <h3 className="font-mono text-sm font-semibold">{side === 'departure' ? 'DEP' : 'ARR'} {flightAirport(selection.flight, side, mode)}</h3>
            {products.map((product) => {
              const reports = data.reports.filter((report) => report.station === station && report.product === product)
              if (!reports.length) return <p className="mt-1 text-sm text-muted-foreground" key={product}>{product} · UNAVAILABLE</p>
              return (
                <div key={product}>
                  {(detailed ? reports : reports.slice(0, 1)).map((report) => (
                    <Report key={report.raw} report={report} full={detailed} />
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
      {data.stations.map((station) => {
        const records = data.notams.filter((record) => record.station === station)
        return (
          <div className="border-b border-border p-3 last:border-b-0" key={station}>
            <h3 className="mb-2 font-mono text-sm font-semibold">{station}</h3>
            {!data.recognized.includes(station) && <p className="text-sm text-muted-foreground">{unknown}</p>}
            {data.recognized.includes(station) && !records.length && (
              <p className="text-sm text-muted-foreground">No applicable or review items in the briefing.</p>
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
