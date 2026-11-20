import { ExternalLink } from 'lucide-react'
import { useCallback, useState } from 'react'
import type { Flight, OpsApi } from './api'
import { Badge, Button, Tabs, TabsContent, TabsList, TabsTrigger } from './components/ui'
import { Resource, utcDate, utcTime } from './presentation'

const sections = [
  { id: 'crew', title: 'CREW / FDP' },
  { id: 'weather', title: 'WX / TAF' },
  { id: 'notams', title: 'NOTAM' },
] as const
const signedMinutes = new Intl.NumberFormat('en', { signDisplay: 'exceptZero', maximumFractionDigits: 1 })

type Section = typeof sections[number]['id']
type FlightRequest = { api: OpsApi; id: number; day: string }

export function Preparation({
  api, day, flight, openLink,
}: { api: OpsApi; day: string; flight: Flight; openLink: (url: string) => void }) {
  const [section, setSection] = useState<Section>('crew')
  const request = { api, id: flight.id, day }
  return (
    <>
      <div className="prep-leg">
        <strong>{flight.number ?? '—'}</strong>
        <span className="font-mono">{flight.departure ?? '—'} / {flight.arrival ?? '—'}</span>
        <time dateTime={flight.std} className="font-mono text-muted-foreground">{utcTime(flight.std)}</time>
      </div>
      <Tabs value={section} onValueChange={(value) => setSection(value as Section)}>
        <TabsList aria-label="Preparation sections">
          {sections.map(({ id, title }) => <TabsTrigger key={id} value={id}>{title}</TabsTrigger>)}
        </TabsList>
        <TabsContent value="crew">{section === 'crew' && <Crew {...request} />}</TabsContent>
        <TabsContent value="weather">{section === 'weather' && <Weather {...request} />}</TabsContent>
        <TabsContent value="notams">{section === 'notams' && <Notams {...request} openLink={openLink} />}</TabsContent>
      </Tabs>
    </>
  )
}

function Crew({ api, id, day }: FlightRequest) {
  const load = useCallback((signal: AbortSignal) => api.crew(id, day, signal), [api, day, id])
  return (
    <Resource load={load} label="crew / FDP">
      {(crew) => (
        <section className="ops-panel">
          <div className="panel-bar"><h2>Crew assignment</h2><Badge>Leon</Badge></div>
          {!crew.members.length ? (
            <p className="px-3 py-3 text-sm text-muted-foreground">
              {crew.available ? 'No crew assignment returned.' : 'Crew source unavailable.'}
            </p>
          ) : (
            <ul className="divide-y divide-border">
              {crew.members.map((member, index) => (
                <li key={index} className="space-y-2 p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    {member.position && <Badge variant="accent">{member.position}</Badge>}
                    <strong>{member.name ?? 'Name unavailable'}</strong>
                  </div>
                  {member.duty?.start && member.duty.end && (
                    <p className="text-sm text-muted-foreground">
                      {member.duty.reporting ? 'Reporting' : 'Roster duty'}{' '}
                      <span className="font-mono text-foreground">
                        {utcTime(member.duty.start)} / {utcDate(member.duty.start) !== utcDate(member.duty.end) ? utcDate(member.duty.end) + ' ' : ''}{utcTime(member.duty.end)}
                      </span>
                    </p>
                  )}
                  {member.duty?.metrics.length ? (
                    <dl className="compact-fields metrics-detail">
                      {member.duty.metrics.map((metric) => (
                        <div className="contents" key={metric.name}>
                          <dt>{metric.name}</dt>
                          <dd className={metric.offence ? 'text-warning' : undefined}>
                            <span className="font-mono">{metric.actual ?? '—'} / {metric.limit ?? '—'}</span>
                            {metric.offence && <Badge variant="warning" className="ml-2">Offence</Badge>}
                          </dd>
                        </div>
                      ))}
                    </dl>
                  ) : <p className="text-sm text-muted-foreground">FDP / rest unavailable.</p>}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
    </Resource>
  )
}

function Weather({ api, id, day }: FlightRequest) {
  const load = useCallback((signal: AbortSignal) => api.weather(id, day, signal), [api, day, id])
  return (
    <Resource load={load} label="METAR / TAF">
      {(weather) => (
        <section className="ops-panel">
          <div className="panel-bar">
            <h2>Latest reports</h2>
            {weather.partial
              ? <Badge variant="warning">Partial</Badge>
              : weather.fetched_at && <Badge>Fetched {utcTime(weather.fetched_at)}</Badge>}
          </div>
          {weather.reports.length ? (
            <ul className="divide-y divide-border">
              {weather.reports.map((report, index) => (
                <li key={index} className="space-y-1.5 p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <strong className="font-mono text-sm">{report.station} {report.product}</strong>
                    <Badge>{report.sources.join(' / ')}</Badge>
                  </div>
                  <pre className="raw-text">{report.raw}</pre>
                </li>
              ))}
            </ul>
          ) : <p className="px-3 py-3 text-sm text-muted-foreground">Weather reports unavailable.</p>}
        </section>
      )}
    </Resource>
  )
}

function Notams({ api, id, day, openLink }: FlightRequest & { openLink: (url: string) => void }) {
  const load = useCallback((signal: AbortSignal) => api.notams(id, day, signal), [api, day, id])
  return (
    <Resource load={load} label="ForeFlight briefing">
      {(data) => {
        const briefingUrl = data.briefing_url
        return <section className="ops-panel">
          <div className="panel-bar"><h2>ForeFlight briefing</h2><Badge>NOTAM</Badge></div>
          <div className="space-y-2 border-b border-border p-3 text-sm text-muted-foreground">
            <p>{data.briefing_time ? `Briefing ${utcDate(data.briefing_time)} · ${utcTime(data.briefing_time)}` : 'Briefing time unavailable.'}</p>
            {!!data.departure_offset_minutes && (
              <p className="text-warning">Departure differs from Leon by {signedMinutes.format(data.departure_offset_minutes)} min.</p>
            )}
            {briefingUrl && (
              <Button variant="outline" className="w-full" onClick={() => openLink(briefingUrl)}>
                <ExternalLink size={15} /> Open briefing
              </Button>
            )}
          </div>
          {data.stations.map((station) => {
            const records = data.notams.filter((record) => record.station === station)
            return (
              <div className="border-b border-border p-3 last:border-b-0" key={station}>
                <h3 className="mb-2 font-mono text-sm font-semibold">{station}</h3>
                {!data.recognized.includes(station) && <p className="text-sm text-muted-foreground">Station section unavailable.</p>}
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
      }}
    </Resource>
  )
}
