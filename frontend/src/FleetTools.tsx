import { ExternalLink, Printer } from 'lucide-react'
import { useCallback, useMemo, useState } from 'react'
import type { OpsApi, Selection } from './api'
import { Badge, Button } from './components/ui'
import { Empty, Facts, ToolPanel, ToolRow, ToolTabs } from './OpsCommon'
import type { Aircraft, FleetAircraft, LegRef } from './ops-types'
import { Resource, utcStamp } from './presentation'

const docTabs = [['aircraft', 'Aircraft'], ['operator', 'Operator'], ['checklist', 'Flight checklist']] as const
type DocTab = typeof docTabs[number][0]

function route(leg: LegRef) {
  return (leg.departure ?? '—') + ' → ' + (leg.arrival ?? '—')
}

export function AircraftPosition({ item, onFlight, showNotes = false }: {
  item: FleetAircraft; onFlight: (leg: LegRef) => void; showNotes?: boolean
}) {
  const position = item.active ? 'Planned airborne · ' + route(item.active) :
    item.last ? 'Last scheduled arrival · ' + (item.last.arrival ?? '—') : 'Scheduled position unavailable.'
  return (
    <div className="tool-section">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="font-mono font-bold">{item.registration}</h3><span className="caption">{item.type}</span>
      </div>
      <p className="mt-1 text-sm">{position}</p>
      {(item.active || item.last) && <p className="minor">Actual position unverified · {utcStamp(item.active?.std ?? item.last?.sta ?? null)}</p>}
      {item.next ? (
        <ToolRow title={(item.next.number ?? 'Next leg') + ' · ' + route(item.next)}
          detail={'Next STD · ' + utcStamp(item.next.std)} action="SKED"
          onClick={() => { if (item.next) onFlight(item.next) }} />
      ) : <p className="minor mt-2">No next leg in the schedule window.</p>}
      {showNotes && item.station && item.notes === null && <p className="minor mt-2">{item.station} notes unavailable.</p>}
      {showNotes && item.station && !!item.notes?.length && <details className="tool-disclosure">
        <summary>{item.station} observations · {item.notes.length}</summary>
        {item.notes.map((note, index) => <div key={index} className="tool-line">
          <p className="minor">{utcStamp(note.observed_at)} · {note.verified ? 'OPS confirmed' : 'Unverified'}{note.pinned && ' · PINNED'}{note.older && ' · HISTORICAL'}</p>
          <p className="mt-1 whitespace-pre-wrap break-words">{note.text}</p>
        </div>)}
      </details>}
    </div>
  )
}

export function OpsBrief({ api, selection, onFlight }: {
  api: OpsApi; selection?: Selection; onFlight: (leg: LegRef) => void
}) {
  return <Resource load={api.brief} label="OPS Brief">
    {(data, refresh, refreshing) => {
      const selected = selection?.flight.aircraft?.toUpperCase()
      const fleet = [...(data.fleet ?? [])].sort((a, b) =>
        Number(b.registration === selected) - Number(a.registration === selected))
      return <ToolPanel title="Fleet brief" source="Leon schedule · OPS station notes" state={data} refresh={refresh} refreshing={refreshing}>
        {data.notes_access === 'unbound' && <Empty>Station notes require a linked OPS account.</Empty>}
        {data.notes_access === 'unavailable' && <Empty>Station notes unavailable.</Empty>}
        {fleet.length ? fleet.map((item) => <AircraftPosition key={item.registration} item={item} onFlight={onFlight}
          showNotes={data.notes_access === 'ready'} />) : <Empty>No aircraft in the Leon schedule window.</Empty>}
      </ToolPanel>
    }}
  </Resource>
}

function AircraftDocuments({ api, aircraft, openLink }: { api: OpsApi; aircraft: Aircraft; openLink: (url: string) => void }) {
  const load = useMemo(() => api.documents(aircraft.id), [api, aircraft.id])
  return <Resource load={load} label={aircraft.registration + ' documents'}>
    {(data, refresh, refreshing) => <ToolPanel title={aircraft.registration + ' documents'} source="Leon" state={data} refresh={refresh} refreshing={refreshing}>
      {data.documents?.length ? data.documents.map((doc, index) => <div className="tool-section" key={index}>
        <div className="flex flex-wrap items-start justify-between gap-2">
          <h3 className="font-bold break-words">{doc.title || doc.type || 'Aircraft document'}</h3>
          {doc.expiry && <Badge variant={Date.parse(doc.expiry) < Date.now() ? 'warning' : 'default'}>EXP {doc.expiry.slice(0, 10)}</Badge>}
        </div>
        <p className="minor">{[doc.title && doc.type, doc.never_expires ? 'No expiry' : !doc.expiry && 'Expiry unreported'].filter(Boolean).join(' · ')}</p>
        {doc.files.length ? doc.files.map((file, i) => <Button key={i} variant="outline" className="mt-2 max-w-full"
          onClick={() => openLink(file.url)}><ExternalLink size={14} /><span className="truncate">{file.name ?? 'Open file'}</span></Button>) :
          <p className="minor mt-2">No approved file attached in Leon.</p>}
      </div>) : <Empty>No documents returned for this aircraft.</Empty>}
    </ToolPanel>}
  </Resource>
}

function AircraftLibrary({ api, selectedReg, openLink }: { api: OpsApi; selectedReg?: string; openLink: (url: string) => void }) {
  const [id, setId] = useState<number | null>()
  return <Resource load={api.aircraft} label="Leon fleet">
    {(data, refresh, refreshing) => {
      const aircraft = data.aircraft ?? []
      const chosen = id === undefined ? aircraft.find((row) => row.registration === selectedReg) :
        aircraft.find((row) => row.id === id)
      return <>
        <ToolPanel title="Aircraft library" source="Leon" state={data} refresh={refresh} refreshing={refreshing}>
          {aircraft.length ? <div className="tool-form">
            <label>Registration<select value={chosen?.id ?? ''} onChange={(event) => setId(Number(event.target.value) || null)}>
              <option value="">Select aircraft</option>
              {aircraft.map((row) => <option value={row.id} key={row.id}>{row.registration}{row.type && ' · ' + row.type}</option>)}
            </select></label>
          </div> : <Empty>No aircraft returned by Leon.</Empty>}
        </ToolPanel>
        {chosen && <AircraftDocuments key={chosen.id} api={api} aircraft={chosen} openLink={openLink} />}
      </>
    }}
  </Resource>
}

function OperatorLibrary({ api }: { api: OpsApi }) {
  return <Resource load={api.operator} label="operator certificates">
    {(data, refresh, refreshing) => <ToolPanel title="Operator certificates" source="Leon AOC register" state={data} refresh={refresh} refreshing={refreshing}>
      {data.certificates?.length ? data.certificates.map((row, i) => <Facts key={i} entries={[
        ['Operator / AOC', row.name], ['Certificate number', row.number], ['Code', row.code],
      ]} />) : <Empty>No operator certificates returned by Leon.</Empty>}
    </ToolPanel>}
  </Resource>
}

function downloadChecklist(selection: Selection, items: { item: string; statuses: string[] }[]) {
  const { flight } = selection
  const text = [flight.number, flight.aircraft, flight.departure + ' → ' + flight.arrival, utcStamp(flight.std),
    'Leon OPS checklist', '', ...items.map(({ item, statuses }) => item + ' • ' + (statuses.join(' / ') || 'Status unreported'))]
  const blob = new Blob([text.filter((line) => line !== null).join('\r\n')], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = 'OPS-checklist-' + flight.id + '.txt'
  document.body.append(link)
  link.click()
  link.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

function SelectedChecklist({ api, selection }: { api: OpsApi; selection: Selection }) {
  const load = useCallback((signal: AbortSignal, refresh?: boolean) => api.checklist(selection, signal, refresh), [api, selection])
  return <Resource load={load} label="flight checklist">
    {(data, refresh, refreshing) => <ToolPanel title="Flight checklist" source="Leon · OPS checklist" state={data} refresh={refresh} refreshing={refreshing}>
      <Facts entries={[[selection.flight.number ?? 'Flight', selection.flight.aircraft], ['STD', utcStamp(selection.flight.std)]]} />
      {data.items?.length ? <>
        <ol>{data.items.map((item, index) => <li className="tool-line" key={index}>
          <strong>{item.item}</strong><p className="minor">{item.statuses.join(' / ') || 'Status unreported'}</p>
        </li>)}</ol>
        <div className="tool-tabs px-3 py-3">
          <Button variant="outline" onClick={() => downloadChecklist(selection, data.items ?? [])}><Printer size={15} /> Download checklist</Button>
        </div>
      </> : <Empty>No checklist items returned for this flight.</Empty>}
    </ToolPanel>}
  </Resource>
}

export function FleetDocs({ api, selection, onSked, openLink, initial = 'aircraft' }: {
  api: OpsApi; selection?: Selection; onSked: () => void; openLink: (url: string) => void; initial?: DocTab
}) {
  const [view, setView] = useState<DocTab>(initial)
  return <>
    <ToolTabs label="Fleet Docs category" active={view} options={docTabs} onChange={setView} />
    {view === 'aircraft' && <AircraftLibrary api={api} selectedReg={selection?.flight.aircraft?.toUpperCase()} openLink={openLink} />}
    {view === 'operator' && <OperatorLibrary api={api} />}
    {view === 'checklist' && (selection ? <SelectedChecklist api={api} selection={selection} /> : (
      <section className="ops-panel p-3"><p className="mb-3 text-sm">Select a flight in SKED to load its Leon checklist.</p>
        <Button onClick={onSked}>Open SKED</Button></section>
    ))}
  </>
}
