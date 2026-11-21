import { AlertTriangle, Check, ClipboardList, Fuel, Mic, PlaneTakeoff, Users, Utensils, Warehouse } from 'lucide-react'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { ApiError, type OpsApi } from './api'
import { Badge, Button } from './components/ui'
import type { GroundDetail, GroundEvent, GroundPreview, Process } from './ops-types'
import { CopyButton, utcTime } from './presentation'
import { Sheet } from './Sheet'

export const eventLabels: Record<string, string> = {
  IN_BLOCK: 'In-block', OUT_BLOCK: 'Off-block', LANDING: 'Landing', TAKEOFF: 'Takeoff', STAND: 'Stand',
  CHOCKS_ON: 'Chocks on', CHOCKS_OFF: 'Chocks off',
  PAX_OFF_STARTED: 'Deplaning started', PAX_OFF_COMPLETED: 'Deplaning complete',
  FUEL_REQUESTED: 'Fuel requested', FUEL_TRUCK_ARRIVED: 'Fuel truck arrived',
  FUEL_STARTED: 'Fuel started', FUEL_COMPLETED: 'Fuel complete',
  CABIN_READY: 'Cabin ready', HANDLING_READY: 'Handling ready', DOCUMENTS_READY: 'Documents ready',
  BOARDING_STARTED: 'Boarding started', BOARDING_COMPLETED: 'Boarding complete',
  PAX_ON_BOARD: 'Passengers on board', REPORTED_READY: 'Reported ready',
  BLOCKER_OPEN: 'Open blocker', BLOCKER_CLOSE: 'Resolve blocker', TARGET: 'UTC target',
  NOTE: 'Turn note', STATION_NOTE: 'Station note',
}

const blockerOptions = [
  { value: 'fuel', label: 'Fuel', reasons: ['Fuel truck queue', 'Awaiting fuel uplift'] },
  { value: 'pax', label: 'PAX Late', reasons: ['Passengers delayed', 'Awaiting baggage'] },
  { value: 'handling', label: 'Handling', reasons: ['Awaiting handling agent', 'Ground equipment unavailable'] },
  { value: 'documents', label: 'Permits / Docs', reasons: ['Awaiting permits', 'Awaiting documents'] },
  { value: 'cabin', label: 'Cabin / Catering', reasons: ['Awaiting catering', 'Cabin service delayed'] },
  { value: 'other', label: 'Slot / ATC', reasons: ['Awaiting slot', 'ATC delay', 'De-icing required'] },
] as const

export const processLabel = (name: Process) => blockerOptions.find((item) => item.value === name)?.label ?? name

function stamps(data: GroundDetail) {
  const marks = new Map<string, GroundDetail['events'][number]>()
  for (const event of data.events) {
    const previous = marks.get(event.kind)
    if (!previous || previous.id < event.id) marks.set(event.kind, event)
  }
  return marks
}

function clientBrief(data: GroundDetail, marks: ReturnType<typeof stamps>) {
  const { turn, timing } = data
  const route = [turn.station, turn.outbound_arrival].filter(Boolean).join('→')
  const stand = marks.get('STAND')?.detail.stand
  const fuel = [marks.get('FUEL_STARTED')?.event_at, marks.get('FUEL_COMPLETED')?.event_at].filter(Boolean).map(utcTime)
  const pax = [marks.get('BOARDING_STARTED')?.event_at, (marks.get('PAX_ON_BOARD') ?? marks.get('BOARDING_COMPLETED'))?.event_at].filter(Boolean).map(utcTime)
  const cabin = marks.get('CABIN_READY')?.event_at
  const handling = marks.get('HANDLING_READY')?.event_at
  const delay = timing.blockers.map((item) => [
    processLabel(item.process), item.reason, 'opened ' + utcTime(item.opened_at),
    item.expected_at && 'ETA ' + utcTime(item.expected_at),
  ].filter(Boolean).join(' · ')).join('; ')
  const head = [turn.registration, route + (turn.planned_out ? ' (STD ' + utcTime(turn.planned_out) + ')' : '')].filter(Boolean).join(' · ')
  return [
    '[' + utcTime(new Date().toISOString()) + '] ' + head, stand,
    timing.in && 'IN ' + utcTime(timing.in), fuel.length && 'Fuel ' + fuel.join('/'),
    cabin && 'Cabin ' + utcTime(cabin), handling && 'Handling ' + utcTime(handling),
    pax.length && 'PAX ' + pax.join('/'), timing.out && 'OUT ' + utcTime(timing.out), delay && 'Delay: ' + delay,
  ].filter(Boolean).join(' | ')
}

function BlockerForm({ busy, error, onSave }: {
  busy: boolean; error: string; onSave: (items: GroundEvent[]) => Promise<boolean>
}) {
  const [process, setProcess] = useState<Process>('fuel')
  const [reason, setReason] = useState('')
  const [eta, setEta] = useState<number>()
  const reasons = blockerOptions.find((value) => value.value === process)?.reasons ?? []
  const expected = useRef<{ eta: number; stamp: string } | null>(null)

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!reason.trim()) return
    if (eta && expected.current?.eta !== eta) {
      expected.current = { eta, stamp: new Date(Date.now() + eta * 60_000).toISOString() }
    }
    void onSave([{ kind: 'BLOCKER_OPEN', process, text: reason.trim(), expected_at: eta ? expected.current?.stamp : undefined }])
  }

  return <form className="tool-form" onSubmit={submit}>
    <div className="chip-grid" role="group" aria-label="Blocked process">
      {blockerOptions.map((item) => <button type="button" key={item.value} className="filter-chip" aria-pressed={process === item.value}
        onClick={() => { setProcess(item.value); setReason('') }}>{item.label}</button>)}
    </div>
    <div className="flex flex-wrap gap-1.5" role="group" aria-label="Blocker reasons">
      {reasons.map((item) => <button type="button" key={item} className="filter-chip" aria-pressed={reason === item} onClick={() => setReason(item)}>{item}</button>)}
    </div>
    <label>Reason<input maxLength={160} required value={reason} onChange={(event) => setReason(event.target.value)} /></label>
    <div className="flex flex-wrap gap-1.5" role="group" aria-label="Expected clear UTC">
      {([['No ETA', undefined], ['+15m', 15], ['+20m', 20], ['+30m', 30], ['+1h', 60]] as const).map(([label, minutes]) =>
        <button type="button" key={label} className="filter-chip" aria-pressed={eta === minutes} onClick={() => {
          expected.current = null
          setEta(minutes)
        }}>{label}</button>)}
    </div>
    {error && <p className="text-warning" role="alert">{error}</p>}
    <Button variant="warning" disabled={busy || !reason.trim()}>{busy ? 'Recording…' : 'Record blocker'}</Button>
  </form>
}

function NoteForm({ busy, error, onSave }: {
  busy: boolean; error: string; onSave: (items: GroundEvent[]) => Promise<boolean>
}) {
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const stand = String(form.get('stand') ?? '').trim()
    const text = String(form.get('note') ?? '').trim()
    const items: GroundEvent[] = []
    if (stand) items.push({ kind: 'STAND', text: stand })
    if (text) items.push({
      kind: form.has('station') ? 'STATION_NOTE' : 'NOTE', text, critical: form.has('critical'),
    })
    if (items.length) void onSave(items)
  }

  return <form className="tool-form" onSubmit={submit}>
    <label>Stand<input name="stand" maxLength={40} placeholder="Stand number or name" /></label>
    <label>Note<textarea name="note" maxLength={500} rows={3} /></label>
    <label className="checkbox-label"><input type="checkbox" name="station" /> Station knowledge base</label>
    <label className="checkbox-label"><input type="checkbox" name="critical" /> Safety observation</label>
    {error && <p className="text-warning" role="alert">{error}</p>}
    <Button disabled={busy}>{busy ? 'Recording…' : 'Save'}</Button>
  </form>
}

type LocalRecognition = {
  processLocally: boolean
  lang: string
  onresult: ((event: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null
  onerror: (() => void) | null
  onend: (() => void) | null
  start: () => void; abort: () => void
}

function localRecognitionClass() {
  const host = window as Window & { SpeechRecognition?: new () => LocalRecognition }
  if (!host.SpeechRecognition || !('processLocally' in host.SpeechRecognition.prototype)) return null
  return host.SpeechRecognition
}

const errorText = (error: unknown) => error instanceof ApiError ? error.message : 'Connection unavailable. Try again.'

function QuickReport({ api, turnKey, saving, onSave }: {
  api: OpsApi; turnKey: string; saving: boolean; onSave: (items: GroundEvent[], requestId?: string) => Promise<boolean>
}) {
  const [text, setText] = useState('')
  const [preview, setPreview] = useState<GroundPreview>()
  const [skipped, setSkipped] = useState<number[]>([])
  const [parsing, setParsing] = useState(false)
  const [message, setMessage] = useState('')
  const [listening, setListening] = useState(false)
  const speech = useRef<LocalRecognition | null>(null)
  const controller = useRef<AbortController | null>(null)
  const canDictate = !!localRecognitionClass()
  useEffect(() => () => {
    controller.current?.abort()
    if (speech.current) {
      speech.current.onend = null
      speech.current.onerror = null
      speech.current.onresult = null
      speech.current.abort()
    }
  }, [])

  function update(value: string) {
    setText(value.slice(0, 600))
    setPreview(undefined)
    setSkipped([])
    setMessage('')
  }

  function dictate() {
    const Recognition = localRecognitionClass()
    if (!Recognition) return
    const recognition = new Recognition()
    recognition.processLocally = true
    recognition.lang = navigator.languages.some((lang) => lang.startsWith('ru')) ? 'ru-RU' : 'en-US'
    recognition.onresult = (event) => update([text, event.results[0]?.[0]?.transcript].filter(Boolean).join(' '))
    recognition.onend = () => { speech.current = null; setListening(false) }
    recognition.onerror = () => setMessage('Dictation unavailable. Use your keyboard to dictate or type.')
    try {
      speech.current = recognition
      setListening(true)
      recognition.start()
    } catch {
      speech.current = null
      setListening(false)
      setMessage('Dictation unavailable. Use your keyboard to dictate or type.')
    }
  }

  async function parse(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!text.trim() || controller.current || saving) return
    const request = new AbortController()
    controller.current = request
    setParsing(true)
    setMessage('')
    setPreview(undefined)
    setSkipped([])
    try {
      const result = await api.parseGround(turnKey, text.trim(), request.signal)
      if (!request.signal.aborted) {
        setPreview(result)
        if (!result.station_notes_allowed) {
          setSkipped(result.items.flatMap((item, index) => item.kind === 'STATION_NOTE' ? [index] : []))
        }
      }
    } catch (error) {
      if (!request.signal.aborted) setMessage(errorText(error))
    } finally {
      controller.current = null
      if (!request.signal.aborted) setParsing(false)
    }
  }

  const items = preview?.items.filter((_item, index) => !skipped.includes(index)) ?? []
  return <section className="ops-panel quick-report">
    <div className="flex items-center gap-2"><Mic size={16} aria-hidden="true" /><h2 className="section-kicker">Qwen Voice / Quick Report</h2></div>
    <form className="report-input" onSubmit={(event) => { void parse(event) }}>
      <textarea aria-label="Quick report" placeholder="Ждём багаж, идёт заправка…" maxLength={600} rows={2}
        value={text} disabled={parsing || saving || listening} onChange={(event) => update(event.target.value)} />
      {canDictate && <Button type="button" variant="outline" size="icon" aria-label="Dictate report"
        disabled={listening || parsing || saving} onClick={dictate}><Mic size={16} /></Button>}
      <Button variant="report" className="report-parse" disabled={parsing || saving || listening || !text.trim()}>{parsing ? 'Parsing…' : 'Parse'}</Button>
    </form>
    {message && <p role="alert" className="text-xs text-warning">{message}</p>}
    {preview && <div className="report-preview">
      <div className="flex items-center justify-between">
        <h3 className="section-kicker">Preview</h3>
        <Button variant="ghost" className="micro-button" disabled={saving} onClick={() => setPreview(undefined)}>Cancel</Button>
      </div>
      {preview.items.map((item, index) => {
        const blocked = item.kind === 'STATION_NOTE' && !preview.station_notes_allowed
        return <label className="preview-item" key={index}>
          <input type="checkbox" checked={!skipped.includes(index)} disabled={saving || blocked} onChange={(event) => {
            setSkipped(event.target.checked ? skipped.filter((i) => i !== index) : [...skipped, index])
          }} />
          <span><strong>{eventLabels[item.kind] ?? item.kind}</strong>
            <small>{[item.event_at && utcTime(item.event_at), item.process && processLabel(item.process), item.text,
              item.expected_at && 'ETA ' + utcTime(item.expected_at), item.target_at && 'Target ' + utcTime(item.target_at), blocked && 'Verified staff required'].filter(Boolean).join(' · ')}</small>
          </span>
        </label>
      })}
      <Button className="w-full" disabled={saving || items.length === 0} onClick={async () => {
        if (await onSave(items, skipped.length ? undefined : preview.request_id)) update('')
      }}>{saving ? 'Recording…' : 'Confirm & record (' + items.length + ')'}</Button>
    </div>}
  </section>
}

export function GroundTurnControls({ data, api, onSaved }: {
  data: GroundDetail; api: OpsApi; onSaved: (detail: GroundDetail) => void
}) {
  const [sheet, setSheet] = useState<'blocker' | 'note'>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const pending = useRef<AbortController | null>(null)
  const retry = useRef<{ key: string; id: string } | undefined>(undefined)
  const { turn, timing } = data
  useEffect(() => () => pending.current?.abort(), [])

  async function save(items: GroundEvent[], previewId?: string) {
    if (pending.current) return false
    const key = JSON.stringify(items)
    if (retry.current?.key !== key) retry.current = { key, id: previewId ?? crypto.randomUUID() }
    const request = new AbortController()
    pending.current = request
    setBusy(true)
    setError('')
    setMessage('')
    try {
      const detail = await api.recordGround(turn.key, { request_id: retry.current.id, items }, request.signal)
      if (request.signal.aborted) return false
      onSaved(detail)
      retry.current = undefined
      setMessage('Recorded')
      return true
    } catch (reason) {
      if (!request.signal.aborted) setError(errorText(reason))
      return false
    } finally {
      pending.current = null
      if (!request.signal.aborted) setBusy(false)
    }
  }

  function saveSheet(items: GroundEvent[]) {
    return save(items).then((saved) => {
      if (saved) setSheet(undefined)
      return saved
    })
  }

  const marks = stamps(data)
  const at = (kind: string) => marks.get(kind)?.event_at
  const fuelStart = at('FUEL_STARTED')
  const fuelEnd = at('FUEL_COMPLETED')
  const boarding = at('BOARDING_STARTED')
  const boarded = at('PAX_ON_BOARD') ?? at('BOARDING_COMPLETED')
  const svcReady = !!at('CABIN_READY') && !!at('HANDLING_READY')
  const processTiles = [
    { title: 'FUEL', Icon: Fuel, start: fuelStart, end: fuelEnd, next: fuelStart ? 'FUEL_COMPLETED' : 'FUEL_STARTED' },
    { title: 'CABIN / CATERING', Icon: Utensils, end: at('CABIN_READY'), next: 'CABIN_READY' },
    { title: 'HANDLING', Icon: Warehouse, end: at('HANDLING_READY'), next: 'HANDLING_READY' },
    { title: 'DOCS (INTERNAL)', Icon: ClipboardList, end: at('DOCUMENTS_READY'), next: 'DOCUMENTS_READY' },
  ]
  const steps = [
    ['IN', !!timing.in], ['FUEL', !!fuelEnd], ['SVC', svcReady], ['PAX', !!boarded], ['OUT', !!timing.out],
  ] as const
  const primary = timing.out ? null : turn.inbound_id && !timing.in ? { kind: 'IN_BLOCK', label: 'IN-BLOCK NOW' }
    : !turn.outbound_id ? null : !boarded ? { kind: 'PAX_ON_BOARD', label: 'PAX ON BOARD' }
    : !timing.ready ? { kind: 'REPORTED_READY', label: 'REPORTED READY' } : { kind: 'OUT_BLOCK', label: 'OFF-BLOCK NOW' }
  const brief = clientBrief(data, marks)

  return <div className="turn-controls">
    <section className="ops-panel readiness">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="section-kicker">Readiness</h2>
          <p className="font-mono text-sm font-semibold">{[turn.inbound_number, turn.outbound_number].filter(Boolean).join(' → ') || turn.station}</p>
        </div>
        <CopyButton text={brief} label="Copy Client Brief" />
      </div>
      <ol className="progress-steps" aria-label="Turn milestones">
        {steps.map(([label, done]) => <li key={label} data-done={done}>
          <span>{done ? <Check size={13} aria-hidden="true" /> : <span aria-hidden="true">·</span>}</span>{label}
          <span className="sr-only">{done ? ' recorded' : ' pending'}</span>
        </li>)}
      </ol>
      <div className="readiness-times">
        <span>{turn.planned_out ? 'STD ' + utcTime(turn.planned_out) : 'ARRIVAL TURN'}</span>
        {marks.get('STAND')?.detail.stand && <span>{marks.get('STAND')?.detail.stand}</span>}
        {timing.ready && <span className="text-primary">READY {utcTime(timing.ready)}</span>}
      </div>
    </section>
    <QuickReport api={api} turnKey={turn.key} saving={busy} onSave={save} />
    <div className="turn-primary">
      <Button onClick={() => { if (primary) void save([{ kind: primary.kind }]) }} disabled={busy || !primary}>
        <PlaneTakeoff size={19} /> {primary?.label ?? (timing.out ? 'TURN COMPLETED' : 'ARRIVAL RECORDED')}
      </Button>
      <Button variant="attention" disabled={busy} onClick={() => { setError(''); setSheet('blocker') }}>
        <AlertTriangle size={18} /> BLOCKER
      </Button>
    </div>
    {message && <p className="minor text-primary" role="status">{message}</p>}
    {!sheet && error && <p className="text-xs text-warning" role="alert">{error}</p>}
    {timing.blockers.length > 0 && <section className="ops-panel open-blockers">
      <h2 className="section-kicker text-warning">Open blockers</h2>
      {timing.blockers.map((item) => <div className="blocker-row" key={item.process}>
        <div>
          <strong>{processLabel(item.process)}{item.reason && ' · ' + item.reason}</strong>
          <p className="minor">Opened {utcTime(item.opened_at)}{item.expected_at ? ' · ETA ' + utcTime(item.expected_at) : ' · ETA unreported'}</p>
        </div>
        <Button variant="outline" className="micro-button text-primary" disabled={busy}
          onClick={() => { void save([{ kind: 'BLOCKER_CLOSE', process: item.process }]) }}><Check size={13} /> Resolve</Button>
      </div>)}
    </section>}
    <section>
      <h2 className="section-kicker mb-2">Parallel processes</h2>
      <div className="process-grid">
        {processTiles.map(({ title, Icon, start, end, next }) => <button className="process-tile" key={title} disabled={busy || !!end}
          data-state={end ? 'done' : start ? 'active' : 'wait'} onClick={() => { void save([{ kind: next }]) }}>
          <span><Icon size={17} aria-hidden="true" /><Badge variant={end ? 'accent' : start ? 'warning' : 'default'}>{end ? 'DONE' : start ? 'ACTIVE' : 'WAIT'}</Badge></span>
          <strong>{title}</strong>
          <small>{start ? utcTime(start) + ' / ' + (end ? utcTime(end) : '…') : end ? utcTime(end) : next === 'FUEL_STARTED' ? 'Start fuel' : 'Mark ready'}</small>
        </button>)}
      </div>
    </section>
    <div className="passenger-row">
      <button className="process-tile passenger-process" disabled={busy || !!boarded || !turn.outbound_id}
        data-state={boarded ? 'done' : boarding ? 'active' : 'wait'} onClick={() => {
          void save([{ kind: boarding ? 'BOARDING_COMPLETED' : 'BOARDING_STARTED' }])
        }}>
        <Users size={17} /><span><strong>PASSENGERS</strong>
          <small>{boarding ? utcTime(boarding) + ' / ' + (boarded ? utcTime(boarded) : '…') : boarded ? utcTime(boarded) : 'Start boarding'}</small>
        </span>
      </button>
      <Button variant="outline" disabled={busy} onClick={() => { setError(''); setSheet('note') }}>NOTE / STAND</Button>
    </div>
    {sheet && <Sheet title={sheet === 'blocker' ? 'Record blocker' : 'Stand / Note'} onClose={() => { setSheet(undefined); setError('') }}>
      {sheet === 'blocker' ? <BlockerForm busy={busy} error={error} onSave={saveSheet} />
        : <NoteForm busy={busy} error={error} onSave={saveSheet} />}
    </Sheet>}
  </div>
}
