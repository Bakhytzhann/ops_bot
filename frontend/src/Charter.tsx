import { ArrowRight, ClipboardList, Plus, RefreshCw } from 'lucide-react'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { ApiError, type Display, type OpsApi } from './api'
import { Badge, Button, Tabs, TabsContent, TabsList, TabsTrigger } from './components/ui'
import { Empty, Facts } from './OpsCommon'
import type {
  CharterAirportProfile, CharterAirportProfileDraft, CharterChainLeg, CharterChainSegment, CharterCrewScenario, CharterEconomics,
  CharterCurrentAircraft, CharterMatrix, CharterMatrixAircraft, CharterQuoteFtl, CharterQuoteFtlMetrics, CharterRate,
  CharterRateCard, CharterRateDraft, CharterRequest,
} from './ops-types'
import { CopyButton, Resource, utcStamp } from './presentation'
import { Sheet } from './Sheet'

const measurement = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 1 })
const sourceLabels: Record<string, string> = {
  foreflight_route: 'ForeFlight Route', foreflight: 'ForeFlight Route', journey_log: 'Journey Log',
  isa_model: 'ISA Model', mixed: 'Разные источники по перелётам', unavailable: 'Источник недоступен',
}
const statusLabels: Record<string, string> = {
  at_departure: 'В аэропорту вылета', ferry_required: 'Нужен перегон', airborne: 'В полёте',
  on_ground: 'На земле', departed: 'Вылетел', arrival_unconfirmed: 'Прилёт не подтверждён',
  clear: 'Без конфликта', available: 'Доступно', verified: 'Подтверждено', confirmed: 'Подтверждено',
  feasible: 'Укладывается', pass: 'Укладывается', fits: 'Укладывается', ok: 'В пределах',
  fits_after_rest: 'После отдыха', projected_after_rest: 'Пересчитать окно после отдыха', roster: 'По графику',
  checked: 'Окно проверено', restricted: 'Ограничение', scheduled_ftl: 'FTL назначенного рейса',
  blocked: 'Ограничение', conflict: 'Конфликт', failed: 'Стоп', fail: 'Стоп', partial: 'Частично',
  limited: 'Ограничение', unknown: 'Уточнить', unavailable: 'Уточнить', unconfirmed: 'Уточнить',
  confirmation_required: 'Подтвердить', no_roster: 'Нужен график экипажей',
  on_duty: 'В duty', resting: 'На отдыхе', mixed: 'Статус по пилотам',
  within_current_limits: 'В пределах остатка',
}
const positiveStatuses = new Set(['at_departure', 'clear', 'available', 'verified', 'confirmed', 'feasible', 'pass', 'fits', 'fits_after_rest', 'ok', 'checked', 'within_current_limits'])

function quoteTime(value: string | null | undefined) {
  return value && Number.isFinite(Date.parse(value)) ? utcStamp(value).replace(/Z$/, ' UTC') : null
}

function localTime(value: string | null | undefined) {
  if (!value) return null
  return value.includes('T') ? value.slice(0, 16).replace('T', ' ') + ' LT' : value.slice(0, 5) + ' LT'
}

function confirmTime(leg: CharterRequest['legs'][number]) {
  if (leg.time_mode === 'WINDOW') {
    if (!leg.window_clock_start && !leg.window_clock_stop) return 'Вылет в течение дня'
    const start = leg.window_clock_start?.slice(0, 5) ?? '00:00'
    const end = leg.window_clock_stop?.slice(0, 5) ?? '24:00'
    const zone = leg.timezone_basis === 'utc' ? 'UTC' : (leg.timezone_basis === 'arrival' ? leg.arr_icao : leg.dep_icao) + ' LT'
    return 'Окно вылета ' + start + ' → ' + end + ' ' + zone
  }
  const time = localTime(leg.target_time_lt) ?? quoteTime(leg.target_time_utc)
  const prefix = leg.time_mode === 'STA' ? 'Прилёт не позднее ' : 'Вылет '
  return prefix + (time ?? '')
}

function duration(minutes: number | null | undefined) {
  if (minutes == null) return null
  const rounded = Math.round(minutes)
  const abs = Math.abs(rounded)
  const hours = Math.floor(abs / 60)
  return (rounded < 0 ? '−' : '') + (hours ? hours + ' ч ' + abs % 60 + ' мин' : abs + ' мин')
}

function dutyDuration(seconds: number | null | undefined) {
  if (seconds == null) return null
  if (seconds > 0 && seconds < 60) return `${Math.floor(seconds)} с`
  return duration(Math.floor(seconds / 60))
}

function money(value: number | null | undefined, currency?: string | null) {
  if (value == null) return null
  return currency
    ? new Intl.NumberFormat('en-GB', { style: 'currency', currency, maximumFractionDigits: 2 }).format(value)
    : measurement.format(value)
}

function statusLabel(status: string) {
  return statusLabels[status.toLowerCase()] ?? status.replaceAll('_', ' ')
}

function Status({ status, title }: { status: string; title?: string }) {
  if (!status) return null
  return <Badge className="max-w-full shrink whitespace-normal" variant={positiveStatuses.has(status.toLowerCase()) ? 'accent' : 'warning'}>
    {title && title + ' · '}{statusLabel(status)}
  </Badge>
}

function Issues({ items }: { items?: string[] }) {
  if (!items?.length) return null
  return <ul className="space-y-1 text-sm text-warning">
    {[...new Set(items)].map((item) => <li key={item} className="wrap-anywhere">{item}</li>)}
  </ul>
}

function useAction() {
  const pending = useRef<AbortController>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => () => pending.current?.abort(), [])

  async function run<T>(request: (signal: AbortSignal) => Promise<T>, onSuccess: (value: T) => void) {
    if (pending.current) return
    const controller = new AbortController()
    pending.current = controller
    setError('')
    setBusy(true)
    try {
      const value = await request(controller.signal)
      if (!controller.signal.aborted) onSuccess(value)
    } catch (cause) {
      if (!controller.signal.aborted) setError(cause instanceof ApiError ? cause.message : 'Связь с OPS недоступна. Повторите запрос.')
    } finally {
      pending.current = null
      if (!controller.signal.aborted) setBusy(false)
    }
  }

  return { run, busy, error, clearError: () => setError('') }
}

function ActionError({ error }: { error: string }) {
  return error ? <p role="alert" className="wrap-anywhere text-sm text-warning">{error}</p> : null
}

function ConfirmRequest({ request, onEdit, onConfirm, busy }: {
  request: CharterRequest; onEdit: () => void; onConfirm: () => void; busy: boolean
}) {
  return <section className="ops-panel" aria-label="Qwen parsed request">
    <div className="panel-bar">
      <h2>QWEN PARSED</h2>
      <Badge>{request.pax} PAX</Badge>
    </div>
    <ol className="divide-y divide-border">
      {request.legs.map((leg, index) => <li key={index} className="tool-section space-y-1">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <strong className="font-mono">{leg.dep_icao} → {leg.arr_icao}</strong>
          <span className="minor">{leg.local_date || leg.date_utc}</span>
        </div>
        <p className="text-sm text-muted-foreground">{confirmTime(leg)}</p>
        {!!leg.via?.length && <p className="minor whitespace-normal">Через {leg.via.join(' → ')}</p>}
      </li>)}
    </ol>
    <div className="tool-section flex flex-wrap items-center gap-2">
      <Button type="button" variant="outline" disabled={busy} onClick={onEdit}>Поправить текст</Button>
      <Button type="button" disabled={busy} onClick={onConfirm}>{busy ? 'Выполняется расчёт…' : 'Confirm & Расчёт'}<ArrowRight size={15} /></Button>
    </div>
  </section>
}

function routeLabel(departure: string | null, arrival: string | null) {
  return [departure, arrival].filter(Boolean).join(' → ')
}

function metric(value: string | null, source: string, samples?: number) {
  if (!value) return null
  return <>{value}<span className="minor block whitespace-normal">{sourceLabels[source] ?? source}{samples ? ' · n=' + samples : ''}</span></>
}

function SegmentFacts({ leg, elapsed }: { leg: CharterChainSegment; elapsed?: number | null }) {
  return <Facts entries={[
    ['Вылет', quoteTime(leg.departure_utc)], ['Прилёт', quoteTime(leg.arrival_utc)],
    ['Дальность', leg.distance_nm == null ? null : measurement.format(leg.distance_nm) + ' NM'],
    ['Блок-время', metric(duration(leg.block_minutes), leg.block_basis, leg.block_samples)],
    ...(elapsed != null && elapsed !== leg.block_minutes ? [['Время в пути', duration(elapsed)] as [string, string | null]] : []),
    ['Расход', metric(leg.fuel_kg == null ? null : measurement.format(leg.fuel_kg) + ' kg', leg.fuel_basis, leg.fuel_samples)],
  ]} />
}

function Leg({ leg, label }: { leg: CharterChainLeg; label: string }) {
  const stops = leg.techstops ?? []
  const segments = leg.segments ?? []
  return <section className="border-b border-border">
    <div className="space-y-1 px-3 pt-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="caption">{label}</p>
        <h4 className="font-mono text-sm font-semibold">{routeLabel(leg.departure, leg.arrival)}</h4>
      </div>
      {leg.route && <p className="raw-text font-mono">{leg.route}</p>}
      {leg.performance_status === 'range_limit' && <Badge variant="warning">ForeFlight · предел дальности</Badge>}
      {leg.time_mode === 'WINDOW' && leg.window_start_utc && leg.window_end_utc &&
        <p className="minor whitespace-normal">Окно · {quoteTime(leg.window_start_utc)} → {quoteTime(leg.window_end_utc)}</p>}
      {!!stops.length && <ul className="space-y-1 text-sm text-warning">
        {stops.map((stop, index) => <li key={index}>
          Техстоп {stop.airport}{[
            [quoteTime(stop.arrival_utc), quoteTime(stop.departure_utc)].filter(Boolean).join(' → '),
            duration(stop.ground_minutes),
          ].filter(Boolean).map((fact) => ' · ' + fact).join('')}
        </li>)}
      </ul>}
      <Issues items={leg.direct_issues} />
      <Issues items={leg.issues} />
    </div>
    <SegmentFacts leg={leg} elapsed={leg.elapsed_minutes} />
    {segments.length > 1 && <details className="tool-disclosure">
      <summary>{segments.length} перелёта по маршруту</summary>
      {segments.map((segment, index) => <div key={index} className="border-t border-border">
        <p className="px-3 pt-2 font-mono text-xs">{routeLabel(segment.departure, segment.arrival)}</p>
        {segment.route && <p className="raw-text px-3 pt-1 font-mono">{segment.route}</p>}
        <SegmentFacts leg={segment} />
      </div>)}
    </details>}
    {!!leg.alternative_chains?.length && <details className="tool-disclosure">
      <summary>Расчётные варианты с техстопами</summary>
      {leg.alternative_chains.map((alternative, index) => <Leg key={index} leg={alternative}
        label={(alternative.techstops ?? []).map((stop) => stop.airport).join(' → ')} />)}
    </details>}
  </section>
}

function Cost({ economics }: { economics: CharterEconomics | null | undefined }) {
  if (!economics) return null
  const { currency } = economics
  return <section aria-label="Экономика" className="border-b border-border">
    <div className="tool-section flex items-center justify-between gap-2">
      <h4 className="font-semibold">Экономика</h4>
      {economics.rate_version != null && <Badge>Rate v{economics.rate_version}</Badge>}
    </div>
    <Facts entries={[
      ['Пассажирские плечи', money(economics.pax_cost, currency)],
      ['Перегоны', money(economics.ferry_cost, currency)],
      ['Ожидание', money(economics.wait_cost, currency)],
      ['Сборы', money(economics.fees_est, currency)],
      ['Топливо', money(economics.fuel_addon, currency)],
      ['Floor', money(economics.floor, currency)], ['Target', money(economics.target, currency)],
    ]} />
    {!!economics.missing.length && <div className="tool-section"><Issues items={economics.missing} /></div>}
  </section>
}

function CrewScenario({ scenario }: { scenario: CharterCrewScenario }) {
  const positioning = scenario.positioning
  const timing = [
    ['Доступен с', quoteTime(scenario.available_from_utc)],
    ['Явка', quoteTime(scenario.report_utc)], ['Конец рабочего периода', quoteTime(scenario.duty_end_utc)],
    ['Конец FDP', quoteTime(scenario.fdp_end_utc)],
    ['Duty на момент явки', duration(scenario.duty_at_report_minutes)],
    ['FDP на момент явки', duration(scenario.fdp_at_report_minutes)],
    ['Остаток duty после плеч', duration(scenario.remaining_minutes)],
    ['Первое ограниченное плечо', scenario.first_limited_segment == null ? null : String(scenario.first_limited_segment)],
    ['Продолжение после отдыха', quoteTime(scenario.earliest_continuation_utc)],
    ['Смена у борта', [scenario.handover_station, quoteTime(scenario.handover_utc)].filter(Boolean).join(' · ')],
    ['Контакт', scenario.contact],
  ].filter((entry): entry is [string, string] => Boolean(entry[1]))
  return <article className="border-t border-border py-2">
    <div className="space-y-1 px-3">
      <div className="flex flex-wrap items-baseline justify-between gap-1.5">
        {(scenario.name || scenario.role) && <strong className="text-sm">{[scenario.name, scenario.role].filter(Boolean).join(' · ')}</strong>}
        <Status status={scenario.status} />
      </div>
      {!!scenario.members?.length && <ul className="space-y-1 text-sm">
        {scenario.members.map((member, index) => <li key={index} className="wrap-anywhere">
          {[member.name, member.role, member.contact].filter(Boolean).join(' · ')}
          {member.fdp_remaining && <p className="minor whitespace-normal">FDP остаток · {member.fdp_remaining}</p>}
          {member.next_eobt && <p className="minor whitespace-normal">Следующий EOBT · {quoteTime(member.next_eobt)}</p>}
          {member.rest_before && <p className="minor whitespace-normal">Отдых до · {[member.rest_before.actual, member.rest_before.limit].filter(Boolean).join(' / ')}</p>}
          {member.rest_after && <p className="minor whitespace-normal">Отдых после · {[member.rest_after.actual, member.rest_after.limit].filter(Boolean).join(' / ')}</p>}
          <Issues items={member.violations} />
        </li>)}
      </ul>}
      <Issues items={scenario.issues} />
    </div>
    {!!timing.length && <Facts entries={timing} />}
    {!!scenario.rest_breaks?.length && <div className="tool-section space-y-1 text-sm">
      <h6 className="caption">Отдых по маршруту</h6>
      {scenario.rest_breaks.map((rest, index) => <p key={index} className="wrap-anywhere">
        {rest.station} · освобождение {quoteTime(rest.previous_release_utc)} · доступен с {quoteTime(rest.available_from_utc)}
        {' · вылет '}{quoteTime(rest.next_departure_utc)}
      </p>)}
    </div>}
    {positioning && <div className="border-t border-border">
      <h6 className="px-3 pt-2 text-sm font-semibold">
        Доставка смены · {positioning.flight_number} · {routeLabel(positioning.departure, positioning.arrival)}
      </h6>
      <Facts entries={[
        ['Вылет', quoteTime(positioning.departure_utc)], ['Прилёт', quoteTime(positioning.arrival_utc)],
        ['Готовность к смене', quoteTime(positioning.ready_utc)],
        ['Зачтено в duty', duration(positioning.counted_duty_minutes)], ['Зачтено в FDP', duration(positioning.counted_fdp_minutes)],
      ]} />
    </div>}
  </article>
}

function quotaMargin(kind: string, minutes: number | null): [string, string | null] {
  if (minutes == null) return ['Запас ' + kind, null]
  return [(minutes < 0 ? 'Превышение ' : 'Запас ') + kind, duration(Math.abs(minutes))]
}

function quoteFtlFacts(metrics: CharterQuoteFtlMetrics, current: boolean): [string, string | null][] {
  return [
    [current ? 'Duty от текущего момента' : 'Duty по квоте', duration(metrics.required_duty_minutes)],
    [current ? 'Доступно duty сейчас' : 'Лимит duty', duration(metrics.duty_limit_minutes)],
    quotaMargin('duty', metrics.duty_margin_minutes),
    [current ? 'FDP от текущего момента' : 'FDP по квоте', duration(metrics.required_fdp_minutes)],
    [current ? 'Доступно FDP сейчас' : 'Лимит FDP', duration(metrics.fdp_limit_minutes)],
    quotaMargin('FDP', metrics.fdp_margin_minutes),
  ]
}

function QuoteFtl({ forecast, title, current = false }: { forecast: CharterQuoteFtl; title: string; current?: boolean }) {
  const periods = forecast.periods ?? []
  const facts: [string, string | null][] = [
    ...(periods.length > 1 ? [] : quoteFtlFacts(forecast, current)),
    ['Явка по расчёту', periods.length > 1 ? null : quoteTime(forecast.report_utc)],
    ['Необходимый отдых', duration(forecast.required_rest_minutes)],
    ['Экипаж доступен с', quoteTime(forecast.available_from_utc)],
    ['Первый ограниченный сегмент', forecast.first_limited_segment == null ? null : String(forecast.first_limited_segment)],
  ]
  const shownFacts = facts.filter(([, value]) => value != null)
  return <div className="border-t border-border">
    <div className="tool-section space-y-1">
      <div className="flex flex-wrap items-center gap-2">
        <h5 className="font-semibold">{title}</h5>
        <Status status={forecast.status} />
      </div>
      {forecast.source && <p className="minor whitespace-normal">{forecast.source}</p>}
      <Issues items={forecast.issues} />
    </div>
    {!!shownFacts.length && <Facts entries={shownFacts} />}
    {periods.length > 1 && periods.map((period, index) => {
      const periodFacts: [string, string | null][] = [
        ['Явка', quoteTime(period.report_utc)], ['Освобождение', quoteTime(period.release_utc)],
        ['Конец FDP', quoteTime(period.fdp_end_utc)], ['Отдых после периода', duration(period.required_rest_minutes)],
        ...quoteFtlFacts(period, false),
      ]
      return <div key={index} className="border-t border-border">
        <h6 className="px-3 py-2 text-sm font-semibold">
          Рабочий период {index + 1} · {period.first_sector === period.last_sector
            ? 'сегмент ' + period.first_sector : 'сегменты ' + period.first_sector + '–' + period.last_sector}
        </h6>
        <Facts entries={periodFacts.filter(([, value]) => value != null)} />
      </div>
    })}
  </div>
}

function Crew({ crew }: { crew: CharterMatrixAircraft['crew'] }) {
  const showRoster = (!crew.quote_ftl && !crew.current_quote_ftl) || crew.working.length > 0 || crew.replacements.length > 0
  return <section aria-label="Экипажи и FTL" className="border-b border-border">
    {crew.current_quote_ftl && <QuoteFtl forecast={crew.current_quote_ftl} current title="FTL квоты для текущего экипажа" />}
    {crew.quote_ftl && <QuoteFtl forecast={crew.quote_ftl} title="FTL квоты для отдохнувшего экипажа" />}
    {showRoster && <div className="tool-section space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="font-semibold">Экипажи на программу</h4>
        <Status status={crew.status} />
      </div>
      {crew.source && <p className="minor whitespace-normal">{[crew.source, quoteTime(crew.as_of_utc)].filter(Boolean).join(' · ')}</p>}
      <Issues items={crew.issues} />
    </div>}
    {!!crew.working.length && <>
      <h5 className="caption px-3 py-2">Рабочий экипаж</h5>
      {crew.working.map((scenario, index) => <CrewScenario key={index} scenario={scenario} />)}
    </>}
    {!!crew.replacements.length && <>
      <h5 className="caption border-t border-border px-3 py-2">Сменные экипажи</h5>
      {crew.replacements.map((scenario, index) => <CrewScenario key={index} scenario={scenario} />)}
    </>}
  </section>
}

const airportRoles: Record<string, string> = {
  departure: 'Вылет', arrival: 'Прилёт', techstop: 'Техстоп', ferry_departure: 'Вылет перегона',
  ferry_arrival: 'Прилёт перегона', dep: 'Вылет', arr: 'Прилёт',
}

function withBriefTimes(brief: string, alternate: string) {
  const flightLines = alternate.split('\n').filter((line) => line.startsWith('Flight: '))
  let flight = 0
  return brief.split('\n').map((line) => line.startsWith('Flight: ') ? (flightLines[flight++] ?? line) : line).join('\n')
}

function Airports({ airports }: { airports: CharterMatrixAircraft['airports'] }) {
  if (!airports.length) return null
  return <section aria-label="Аэропорты" className="border-b border-border">
    <div className="tool-section"><h4 className="font-semibold">Окна аэропортов и пермиты</h4></div>
    {airports.map((port, index) => <div key={index} className="border-b border-border last:border-b-0">
      <div className="flex flex-wrap items-center justify-between gap-2 px-3 pt-2">
        <p className="font-mono text-sm">{port.airport} <span className="minor">{airportRoles[port.role] ?? port.role}</span></p>
        <Status status={port.status} />
      </div>
      <Facts entries={[
        ['Время', localTime(port.time_lt) ?? quoteTime(port.time_utc)], ['Рабочее окно UTC', port.opening_hours_utc],
        ['Curfew UTC', port.curfew_utc],
        ['Срок пермита', port.permit_lead_days == null ? null : port.permit_lead_days + ' дн.'],
      ]} />
      {!!port.issues.length && <div className="px-3 pb-2"><Issues items={port.issues} /></div>}
    </div>)}
  </section>
}

function Brief({ api, row }: { api: OpsApi; row: CharterMatrixAircraft }) {
  const [display, setDisplay] = useState<Display>(row.offer_brief_utc && !row.offer_brief ? 'UTC' : 'LT')
  const [instruction, setInstruction] = useState('')
  const [pendingPreview, setPendingPreview] = useState('')
  const [confirmed, setConfirmed] = useState<Partial<Record<Display, string>>>({})
  const action = useAction()
  const brief = confirmed[display] ?? (display === 'LT' ? row.offer_brief : row.offer_brief_utc) ?? ''

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!brief || !instruction.trim()) return
    setPendingPreview('')
    void action.run((signal) => api.charterRefine(brief, instruction.trim(), signal), (result) => {
      setPendingPreview(result.brief)
    })
  }

  function switchTime(mode: Display) {
    setDisplay(mode)
    setPendingPreview('')
    action.clearError()
  }

  return <section aria-label="Offer Brief">
    <div className="panel-bar flex-wrap">
      <h4>Offer Brief</h4>
      <div className="tool-tabs" role="group" aria-label="Время в Offer Brief">
        {(['LT', 'UTC'] as const).map((mode) => <Button key={mode} type="button"
          variant={display === mode ? 'default' : 'outline'} aria-pressed={display === mode}
          disabled={action.busy} onClick={() => switchTime(mode)}>{mode}</Button>)}
      </div>
    </div>
    {!brief ? <Empty>Для клиента пока нет рассчитанного Brief.</Empty> : <>
      <pre className="raw-text border-b border-border bg-background/50 p-3">{brief}</pre>
      <div className="tool-section"><CopyButton key={brief} label="Copy Offer Brief" text={brief} /></div>
      <form className="tool-form" onSubmit={submit} aria-busy={action.busy}>
        <label>Правка через Qwen
          <input value={instruction} maxLength={600} onChange={(event) => {
            setInstruction(event.target.value)
            setPendingPreview('')
            action.clearError()
          }} disabled={action.busy} placeholder="Например, убери цену" required />
        </label>
        <ActionError error={action.error} />
        <Button type="submit" variant="outline" className="justify-self-start" disabled={action.busy || !instruction.trim()}>
          {action.busy ? 'Подготовка…' : 'Предпросмотр правки'}
        </Button>
      </form>
      {pendingPreview && <div className="tool-section space-y-2">
        <h5 className="caption">Предпросмотр правки</h5>
        <pre className="raw-text rounded-md border border-border bg-background p-2.5">{pendingPreview}</pre>
        <div className="flex flex-wrap gap-2">
          <Button type="button" onClick={() => {
            const other = display === 'LT' ? 'UTC' : 'LT'
            const alternate = (other === 'UTC' ? row.offer_brief_utc : row.offer_brief) ?? ''
            setConfirmed({ [display]: pendingPreview, [other]: withBriefTimes(pendingPreview, alternate) })
            setPendingPreview('')
            setInstruction('')
          }}>Confirm правку</Button>
          <Button type="button" variant="outline" onClick={() => setPendingPreview('')}>Отмена</Button>
        </div>
      </div>}
    </>}
  </section>
}

function AircraftDetail({ api, row, focus }: { api: OpsApi; row: CharterMatrixAircraft; focus: CharterRequest['focus'] }) {
  const title = [row.registration, row.aircraft_type].filter(Boolean).join(' · ')
  return <section className="ops-panel" aria-label={title}>
    <div className="panel-bar flex-wrap">
      <h3 className="font-mono">{title}</h3>
      {row.ops_brief && <CopyButton key={row.ops_brief} label="Копировать для OPS" text={row.ops_brief} />}
    </div>
    {focus === 'crew' && <Crew crew={row.crew} />}
    <div className="tool-section space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <Status title="SKED" status={row.schedule.status} />
        <Status status={row.position.projected_status} />
      </div>
      <Issues items={row.schedule.issues} />
      {!!row.schedule.conflicts.length && <ul className="space-y-1 text-sm text-warning">
        {row.schedule.conflicts.map((conflict, index) => <li key={index} className="wrap-anywhere">
          {[conflict.flight_number, routeLabel(conflict.departure, conflict.arrival), quoteTime(conflict.departure_utc),
            quoteTime(conflict.arrival_utc)].filter(Boolean).join(' · ')}
        </li>)}
      </ul>}
    </div>
    <Facts entries={[
      ['Позиция сейчас', row.position.current_station ?? statusLabel(row.position.current_status)],
      ['Позиция к заявке', row.position.planned_station ?? statusLabel(row.position.projected_status)],
      ...(row.position.as_of_utc ? [['Позиция на UTC', quoteTime(row.position.as_of_utc)] as [string, string | null]] : []),
      ...(row.position.planned_for_utc ? [['Заявка на UTC', quoteTime(row.position.planned_for_utc)] as [string, string | null]] : []),
    ]} />
    {row.ferry.map((leg, index) => <Leg key={index} leg={leg} label="FERRY" />)}
    {row.legs.map((leg, index) => <Leg key={index} leg={leg} label={'Плечо ' + (index + 1)} />)}
    {focus !== 'crew' && <Crew crew={row.crew} />}
    <Airports airports={row.airports} />
    <Cost economics={row.economics} />
    <Brief api={api} row={row} />
  </section>
}

function AircraftCard({ row, active, onSelect }: { row: CharterMatrixAircraft; active: boolean; onSelect: () => void }) {
  const ports = row.airports.map((port) => port.status.toLowerCase())
  const portStatus = ports.find((status) => ['blocked', 'conflict', 'failed', 'fail', 'restricted'].includes(status))
    ?? ports.find((status) => !positiveStatuses.has(status))
    ?? (row.airports.some((port) => port.permit_lead_days == null) ? 'partial' : ports[0])
  const economics = row.economics
  const forecast = row.crew.current_quote_ftl ?? row.crew.quote_ftl
  const multiplePeriods = (forecast?.periods?.length ?? 0) > 1
  const requiredDuty = multiplePeriods ? null : duration(forecast?.required_duty_minutes)
  const dutyLimit = multiplePeriods ? null : duration(forecast?.duty_limit_minutes)
  const margin = forecast && !multiplePeriods ? quotaMargin('duty', forecast.duty_margin_minutes) : null
  const position = [row.position.current_station, row.position.planned_station]
  const techstops = row.legs.flatMap((leg) => leg.techstops?.map((stop) => stop.airport) ?? [])
  const sources = [...new Set(row.legs.flatMap((leg) => [leg.block_basis, leg.fuel_basis]))]
    .filter((source) => source && source !== 'unavailable')
  const crewOutcomes = [
    row.crew.working.some((scenario) => scenario.status === 'fits') && 'Одним экипажем',
    row.crew.working.some((scenario) => scenario.status === 'fits_after_rest') && 'После отдыха',
    row.crew.replacements.some((scenario) => scenario.status === 'fits') && 'Со сменой',
  ].filter(Boolean)
  return <button type="button" aria-pressed={active} onClick={onSelect}
    className="charter-aircraft-card">
    <span className="flex flex-wrap items-baseline justify-between gap-x-2 gap-y-1">
      <strong className="font-mono text-base">{row.registration}</strong>
      {row.aircraft_type && <span className="minor">{row.aircraft_type}</span>}
    </span>
    {(position[0] || position[1]) && <span className="flex flex-wrap items-center gap-2 text-xs">
      {position[0] && <span>Сейчас <b className="font-mono">{position[0]}</b></span>}
      {position[1] && <span>К заявке <b className="font-mono">{position[1]}</b></span>}
    </span>}
    {!!row.ferry.length && <span className="wrap-anywhere text-xs text-warning">
      FERRY · {row.ferry.map((leg) => routeLabel(leg.departure, leg.arrival)).join(' / ')}
    </span>}
    {!!techstops.length && <span className="wrap-anywhere text-xs text-warning">Техстоп · {techstops.join(' → ')}</span>}
    {!!sources.length && <span className="minor whitespace-normal">{sources.map((source) => sourceLabels[source] ?? source).join(' · ')}</span>}
    <span className="flex flex-wrap gap-1">
      <Status title="SKED" status={row.schedule.status} />
      <Status title={row.crew.current_quote_ftl ? 'Текущий экипаж' : forecast ? 'FTL квоты' : 'FTL'}
        status={forecast?.status ?? row.crew.status} />
      {portStatus && <Status title="PERMIT" status={portStatus} />}
    </span>
    {(requiredDuty || dutyLimit) && <span className="text-xs text-muted-foreground">
      {[requiredDuty && 'Duty ' + requiredDuty, dutyLimit && (row.crew.current_quote_ftl ? 'доступно ' : 'лимит ') + dutyLimit,
        margin?.[1] && margin[0] + ' ' + margin[1]].filter(Boolean).join(' · ')}
    </span>}
    {multiplePeriods && <span className="text-xs text-muted-foreground">Рабочих периодов · {forecast?.periods?.length}</span>}
    {!!crewOutcomes.length && <span className="text-xs text-primary">{crewOutcomes.join(' · ')}</span>}
    {(economics?.floor != null || economics?.target != null) && <span className="font-mono text-xs">
      {economics?.floor != null && <span className="text-muted-foreground">Floor {money(economics.floor, economics.currency)}</span>}
      {economics?.target != null && <span className="ml-2 text-primary">Target {money(economics.target, economics.currency)}</span>}
    </span>}
  </button>
}

function Matrix({ api, data }: { api: OpsApi; data: CharterMatrix }) {
  const [selected, setSelected] = useState<string>()
  const detail = useRef<HTMLDivElement>(null)
  const row = data.aircraft.find((item) => item.registration === selected)
  useEffect(() => {
    if (!selected) return
    detail.current?.scrollIntoView({ block: 'start' })
    detail.current?.focus({ preventScroll: true })
  }, [selected])

  return <div className="space-y-3">
    <section className="ops-panel" aria-label="Матрица флота">
      <div className="panel-bar flex-wrap">
        <div>
          <h2>Матрица флота</h2>
          {data.as_of_utc && <p className="minor">SKED · {quoteTime(data.as_of_utc)}</p>}
        </div>
        {!data.schedule_complete && <Badge variant="warning">SKED · частично</Badge>}
      </div>
      {!data.aircraft.length ? <Empty>Активный флот не получен.</Empty> :
        <div className="grid gap-2.5 p-3 sm:grid-cols-2">
          {data.aircraft.map((item) => <AircraftCard key={item.registration} row={item}
            active={item.registration === selected} onSelect={() => setSelected(item.registration)} />)}
        </div>}
    </section>
    {row && <div ref={detail} tabIndex={-1} className="charter-aircraft-detail">
      <AircraftDetail key={row.registration} api={api} row={row} focus={data.request.focus} />
    </div>}
  </div>
}

const currentCrewReasons: Record<string, string> = {
  incomplete_schedule: 'Leon вернул расписание частично.',
  incomplete_flight_watch: 'Leon вернул фактические отметки рейсов частично.',
  no_observed_movement: 'В Flight Watch ещё нет подтверждённого движения борта.',
  assignment_unavailable: 'Leon не вернул состав экипажа на фактический рейс.',
  no_assignment_on_observed_flight: 'На фактический рейс экипаж в Leon не указан.',
  current_duty_unconfirmed: 'Фактический рейс известен. Текущий duty экипажа не подтверждён.',
  member_ftl_incomplete: 'Leon вернул текущие лимиты только для части экипажа.',
}

function CurrentAircraft({ row }: { row: CharterCurrentAircraft }) {
  const { crew, position } = row
  const crewNames = crew.members.map((member) => member.name || member.role).filter(Boolean)
  const dutyMembers = crew.members.filter((member) => member.status === 'on_duty')
  const knownCrewFacts: [string, (number | null | undefined)[]][] = dutyMembers.length
    ? [
        ['Остаток duty', dutyMembers.map((member) => member.duty_remaining_seconds)],
        ['Остаток FDP', dutyMembers.map((member) => member.fdp_remaining_seconds)],
      ]
    : []
  const remaining = knownCrewFacts.flatMap(([label, values]) => {
    const measured = values.filter((value): value is number => value != null)
    if (!measured.length) return []
    const coverage = measured.length < crew.members.length ? ` (${measured.length}/${crew.members.length})` : ''
    return [`${label} ${dutyDuration(Math.min(...measured))}${coverage}`]
  })
  const reason = crew.reason && currentCrewReasons[crew.reason]
  return <details className="tool-disclosure">
    <summary className="cursor-pointer focus-visible:outline-primary">
      <span className="font-mono">{[row.registration, row.aircraft_type].filter(Boolean).join(' · ')}</span>
      <span className="ml-2 align-middle"><Status status={crew.status} /></span>
      <span className="mt-1 block text-xs font-normal text-muted-foreground">
        {[position.current_station ?? statusLabel(position.current_status), ...crewNames].filter(Boolean).join(' · ')}
      </span>
      {!!remaining.length && <span className="mt-1 block text-xs font-normal">{remaining.join(' · ')}</span>}
    </summary>
    {(crew.source || crew.as_of_utc || crew.flight) && <p className="px-3 pb-2 text-xs text-muted-foreground">
      {[crew.source, quoteTime(crew.as_of_utc), crew.flight?.number,
        crew.flight && routeLabel(crew.flight.departure ?? null, crew.flight.arrival ?? null)].filter(Boolean).join(' · ')}
    </p>}
    {reason && <p className="px-3 pb-2 text-sm text-muted-foreground">{reason}</p>}
    {crew.members.map((member, index) => {
      const facts: [string, string | null][] = [
        ['Начало duty', quoteTime(member.duty_start_utc)],
        ['Отработано', dutyDuration(member.duty_elapsed_seconds)],
        ['Остаток duty', dutyDuration(member.duty_remaining_seconds)],
        ['Лимит наступает', quoteTime(member.duty_limit_utc)],
        ['Начало FDP', quoteTime(member.fdp_start_utc)],
        ['Прошло FDP', dutyDuration(member.fdp_elapsed_seconds)],
        ['Остаток FDP', dutyDuration(member.fdp_remaining_seconds)],
        ['Лимит FDP наступает', quoteTime(member.fdp_limit_utc)],
        ['Доступен после отдыха', quoteTime(member.rest_until_utc)],
        ['Следующий EOBT', quoteTime(member.next_eobt_utc)],
      ]
      const shownFacts = facts.filter(([, value]) => value != null)
      return <div key={index} className="border-t border-border">
        <div className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
          <strong className="text-sm">{[member.name, member.role].filter(Boolean).join(' · ') || 'Член экипажа'}</strong>
          <Status status={member.status} />
        </div>
        {!!shownFacts.length && <Facts entries={shownFacts} />}
      </div>
    })}
    {!crew.members.length && !reason && <Empty>Оперативный экипаж не подтверждён.</Empty>}
  </details>
}

function CurrentFleet({ api }: { api: OpsApi }) {
  return <section className="ops-panel" aria-label="Текущий статус бортов">
    <Resource load={api.charterCurrentFleet} label="текущий статус бортов" pending={
      <div className="panel-bar"><h2>Текущий статус бортов</h2></div>
    }>
      {(data, refresh, refreshing) => <>
        <div className="panel-bar flex-wrap">
          <div>
            <h2>Текущий статус бортов</h2>
            {data.as_of_utc && <p className="minor">Снимок {quoteTime(data.as_of_utc)}</p>}
          </div>
          <span className="flex flex-wrap items-center gap-2">
            {(data.schedule_complete ?? data.complete) === false && <Badge variant="warning">Данные частичные</Badge>}
            <Button type="button" variant="outline" onClick={refresh} disabled={refreshing}
              aria-label="Обновить текущий статус бортов"><RefreshCw size={14} /></Button>
          </span>
        </div>
        {data.status === 'UNAVAILABLE' ? <Empty>Текущий статус бортов недоступен.</Empty> :
        !data.aircraft?.length ? <Empty>Активный флот не получен.</Empty> :
          data.aircraft.map((row) => <CurrentAircraft key={row.registration} row={row} />)}
      </>}
    </Resource>
  </section>
}

function QuoteDesk({ api }: { api: OpsApi }) {
  const [raw, setRaw] = useState('')
  const [draft, setDraft] = useState<CharterRequest | null>(null)
  const [matrix, setMatrix] = useState<CharterMatrix | null>(null)
  const [clarification, setClarification] = useState('')
  const input = useRef<HTMLTextAreaElement>(null)
  const action = useAction()

  function parse(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const text = raw.trim()
    if (!text || draft || matrix) return
    setClarification('')
    void action.run((signal) => api.charterParse(text, signal), (result) => {
      setDraft(result.request)
      setClarification(result.clarification ?? '')
    })
  }

  function calculate() {
    if (!draft || matrix) return
    void action.run((signal) => api.charterMatrix(draft, signal), (result) => {
      setMatrix(result)
      setDraft(null)
    })
  }

  function edit() {
    setDraft(null)
    setMatrix(null)
    setClarification('')
    action.clearError()
    requestAnimationFrame(() => input.current?.focus())
  }

  return <div className="space-y-3">
    <CurrentFleet api={api} />
    <section className="ops-panel">
      <div className="panel-bar">
        <h2 className="flex items-center gap-2"><ClipboardList size={16} aria-hidden="true" className="text-primary" />Заявка на чартер</h2>
        {matrix && <Button type="button" variant="outline" onClick={edit}>Изменить</Button>}
      </div>
      <form className="tool-form" aria-busy={action.busy} onSubmit={parse}>
        <label>Текст задачи
          <textarea ref={input} value={raw} onChange={(event) => {
            setRaw(event.target.value)
            setClarification('')
            action.clearError()
          }} rows={4} maxLength={2600} readOnly={Boolean(draft || matrix)} disabled={action.busy} required
            placeholder="Алматы — Дубай, в пятницу к 13:00 по Дубаю, 6 паксов. Обратно в воскресенье. Проверить, можно ли одним экипажем." />
        </label>
        {clarification && <p role="status" className="wrap-anywhere text-sm text-warning">{clarification}</p>}
        <ActionError error={action.error} />
        {!draft && !matrix && <Button type="submit" className="justify-self-start" disabled={action.busy || !raw.trim()}>
          {action.busy ? 'Разбираю задачу…' : 'Разобрать задачу'}
        </Button>}
      </form>
    </section>
    {draft && <ConfirmRequest request={draft} onConfirm={calculate} onEdit={edit} busy={action.busy} />}
    {matrix && <Matrix api={api} data={matrix} />}
  </div>
}

function OpportunityDesk({ api }: { api: OpsApi }) {
  return <Resource load={api.charterOpportunities} label="рынок перегонов">
    {(data, refresh, refreshing) => <div className="space-y-3">
      <section className="ops-panel">
        <div className="panel-bar flex-wrap">
          <div><h2>Empty Legs</h2><p className="minor">SKED · {quoteTime(data.as_of_utc)}</p></div>
          <div className="flex items-center gap-2">
            {!data.schedule_complete && <Badge variant="warning">SKED · частично</Badge>}
            <Button type="button" variant="ghost" size="icon" aria-label="Обновить Empty Legs"
              disabled={refreshing} onClick={refresh}><RefreshCw size={16} /></Button>
          </div>
        </div>
        {!data.empty_legs.length && <Empty>Подтверждённых перегонов в расписании нет.</Empty>}
        {data.empty_legs.map((leg, index) => {
          const route = routeLabel(leg.departure, leg.arrival)
          const dates = [quoteTime(leg.departure_utc), quoteTime(leg.arrival_utc)].filter(Boolean).join(' → ')
          const brief = ['Empty Leg · ' + route, dates, leg.aircraft_type].filter(Boolean).join('\n')
          return <article className="tool-section flex flex-wrap items-center justify-between gap-2" key={index}>
            <div className="min-w-0">
              <h3 className="font-mono text-sm font-semibold">{route}</h3>
              <p className="minor whitespace-normal">{dates}</p>
              <p className="minor whitespace-normal">{[leg.registration, leg.aircraft_type, leg.flight_number].filter(Boolean).join(' · ')}</p>
            </div>
            <CopyButton label="Copy Empty Leg" text={brief} />
          </article>
        })}
      </section>
      <section className="ops-panel">
        <div className="panel-bar"><h2>Open Windows</h2><Badge>≥ 24 ч</Badge></div>
        {!data.open_windows.length && <Empty>Окон простоя от 24 часов в расписании нет.</Empty>}
        {data.open_windows.map((window, index) => <article key={index} className="tool-section">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="font-mono text-sm font-semibold">{window.registration} · {window.station}</h3>
            <Badge>{duration(window.minutes)}</Badge>
          </div>
          <p className="minor whitespace-normal">{quoteTime(window.start_utc)} → {quoteTime(window.end_utc)}</p>
        </article>)}
      </section>
    </div>}
  </Resource>
}

function RateDetails({ rate }: { rate: CharterRate }) {
  const fuel = money(rate.fuel_price_per_kg, rate.currency)
  return <Facts entries={[
    ['Пассажирский час', money(rate.pax_hour_rate, rate.currency)],
    ['Час перегона', money(rate.ferry_hour_rate, rate.currency)],
    ['Ожидание за ночь', money(rate.overnight_fee, rate.currency)],
    ['Минимум в сутки', rate.daily_min_hours == null ? null : measurement.format(rate.daily_min_hours) + ' ч'],
    ['Топливо', rate.fuel_included ? 'Включено' : fuel && fuel + '/kg'],
    ['Маржа', measurement.format(rate.margin_pct) + '%'],
    ['Действует с', quoteTime(rate.valid_from)],
    ...(rate.valid_to ? [['Действует до', quoteTime(rate.valid_to)] as [string, string | null]] : []),
    ...(rate.updated_by ? [['Автор', rate.updated_by] as [string, string]] : []),
    ...(rate.comment ? [['Комментарий', rate.comment] as [string, string]] : []),
  ]} />
}

function RateEditor({ api, rate, onClose, onSaved }: {
  api: OpsApi; rate: CharterRate | null; onClose: () => void; onSaved: (snapshot: CharterRateCard) => void
}) {
  const [scope, setScope] = useState(rate?.aircraft_type ? 'aircraft_type' : 'registration')
  const [fuel, setFuel] = useState(rate ? (rate.fuel_included ? 'wet' : 'dry') : '')
  const action = useAction()

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const value = (key: string) => String(form.get(key) ?? '').trim()
    const scopeValue = value('scope_key').toUpperCase()
    const optionalNumber = (key: string) => value(key) === '' ? null : Number(value(key))
    const draft: CharterRateDraft = {
      registration: scope === 'registration' ? scopeValue : null,
      aircraft_type: scope === 'aircraft_type' ? scopeValue : null,
      currency: value('currency') === 'EUR' ? 'EUR' : 'USD',
      pax_hour_rate: Number(value('pax_hour_rate')),
      ferry_hour_rate: Number(value('ferry_hour_rate')),
      overnight_fee: Number(value('overnight_fee')),
      daily_min_hours: optionalNumber('daily_min_hours'),
      fuel_included: fuel === 'wet',
      fuel_price_per_kg: fuel === 'dry' ? optionalNumber('fuel_price_per_kg') : null,
      margin_pct: Number(value('margin_pct')),
      comment: value('comment') || null,
    }
    void action.run((signal) => api.saveCharterRate(draft, signal), onSaved)
  }

  const numeric = [
    ['pax_hour_rate', 'Пассажирский час', true],
    ['ferry_hour_rate', 'Час перегона', true],
    ['overnight_fee', 'Ожидание за ночь', true],
    ['daily_min_hours', 'Минимум часов в сутки', false],
    ['margin_pct', 'Маржа (%)', true],
  ] as const

  return <Sheet title={rate ? 'Новая версия ставки' : 'Новая ставка'} onClose={onClose}>
    <form onSubmit={save} aria-busy={action.busy}>
      <fieldset className="tool-form" disabled={action.busy}>
        <div className="grid grid-cols-2 gap-2">
          <label>Привязка
            <select value={scope} disabled={Boolean(rate)} onChange={(event) => setScope(event.target.value)}>
              <option value="registration">Борт</option><option value="aircraft_type">Тип ВС</option>
            </select>
          </label>
          <label>{scope === 'registration' ? 'Регистрация' : 'Код типа ВС'}
            <input name="scope_key" defaultValue={rate?.registration ?? rate?.aircraft_type ?? ''}
              className="uppercase" autoCapitalize="characters" maxLength={scope === 'registration' ? 16 : 32}
              spellCheck={false} readOnly={Boolean(rate)} required />
          </label>
        </div>
        <label>Валюта
          <select name="currency" defaultValue={rate?.currency ?? ''} required>
            <option disabled value="">Выбрать</option><option value="USD">USD</option><option value="EUR">EUR</option>
          </select>
        </label>
        <div className="grid grid-cols-2 gap-2">
          {numeric.map(([name, label, required]) => <label key={name}>{label}
            <input type="number" name={name} inputMode="decimal" min={0} max={name === 'daily_min_hours' ? 24 : undefined}
              step="any" required={required} defaultValue={rate?.[name] ?? ''} />
          </label>)}
        </div>
        <label>Топливо
          <select value={fuel} required onChange={(event) => setFuel(event.target.value)}>
            <option disabled value="">Выбрать</option>
            <option value="wet">Включено (wet)</option><option value="dry">Отдельно (dry)</option>
          </select>
        </label>
        {fuel === 'dry' && <label>Цена топлива за kg в валюте ставки
          <input type="number" name="fuel_price_per_kg" inputMode="decimal" min={0} step="any" defaultValue={rate?.fuel_price_per_kg ?? ''} />
        </label>}
        <label>Комментарий<textarea name="comment" rows={2} maxLength={500} defaultValue={rate?.comment ?? ''} /></label>
        <ActionError error={action.error} />
        <Button type="submit" className="justify-self-start">{action.busy ? 'Сохранение…' : 'Сохранить версию'}</Button>
      </fieldset>
    </form>
  </Sheet>
}

function AirportProfileEditor({ api, profile, onClose, onSaved }: {
  api: OpsApi; profile: CharterAirportProfile | null; onClose: () => void; onSaved: (snapshot: CharterRateCard) => void
}) {
  const [scope, setScope] = useState<CharterAirportProfile['scope_type']>(profile?.scope_type ?? 'icao')
  const action = useAction()

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const text = (key: string) => String(form.get(key) ?? '').trim() || null
    const number = (key: string) => {
      const value = text(key)
      return value === null ? null : Number(value)
    }
    const currency = text('currency')
    const draft: CharterAirportProfileDraft = {
      scope_type: scope, scope_key: profile?.scope_key ?? text('scope_key') ?? '',
      currency: currency === 'USD' || currency === 'EUR' ? currency : null,
      handling_landing_est: number('handling_landing_est'), pax_fee_per_head: number('pax_fee_per_head'),
      overflight_avg_per_hr: number('overflight_avg_per_hr'), permit_lead_days: number('permit_lead_days'),
      operating_hours_utc: text('operating_hours_utc'), curfew_utc: text('curfew_utc'),
      techstop_turn_minutes: number('techstop_turn_minutes'), comment: text('comment'),
    }
    void action.run((signal) => api.saveCharterRate(draft, signal), onSaved)
  }

  const numbers = [
    ['handling_landing_est', 'Handling / landing', 0, 'any'],
    ['pax_fee_per_head', 'Сбор с пассажира', 0, 'any'],
    ['overflight_avg_per_hr', 'Пролёт за час', 0, 'any'],
    ['permit_lead_days', 'Срок пермита в днях', 0, '1'],
    ['techstop_turn_minutes', 'Техстоп в минутах', 1, '1'],
  ] as const

  return <Sheet title={profile ? 'Новая версия профиля' : 'Новый профиль аэропорта'} onClose={onClose}>
    <form onSubmit={save} aria-busy={action.busy}>
      <fieldset className="tool-form" disabled={action.busy}>
        <div className="grid grid-cols-2 gap-2">
          <label>Привязка
            <select value={scope} disabled={Boolean(profile)} onChange={(event) => setScope(event.target.value === 'category' ? 'category' : 'icao')}>
              <option value="icao">ICAO</option><option value="category">Категория</option>
            </select>
          </label>
          {scope === 'icao' ? <label>ICAO
            <input name="scope_key" defaultValue={profile?.scope_key ?? ''} readOnly={Boolean(profile)}
              className="uppercase" autoCapitalize="characters" maxLength={4} pattern="[A-Za-z]{4}" spellCheck={false} required />
          </label> : <label>Категория
            <select name="scope_key" defaultValue={profile?.scope_key ?? ''} disabled={Boolean(profile)} required>
              <option disabled value="">Выбрать</option>
              <option value="domestic">Domestic</option><option value="regional">Regional</option><option value="international">International</option>
            </select>
          </label>}
        </div>
        <label>Валюта сборов
          <select name="currency" defaultValue={profile?.currency ?? ''}>
            <option value="">Выбрать</option><option value="USD">USD</option><option value="EUR">EUR</option>
          </select>
        </label>
        <div className="grid grid-cols-2 gap-2">
          {numbers.map(([name, label, min, step]) => <label key={name}>{label}
            <input type="number" name={name} inputMode={step === '1' ? 'numeric' : 'decimal'}
              min={min} step={step} defaultValue={profile?.[name] ?? ''} />
          </label>)}
        </div>
        <div className="grid grid-cols-2 gap-2">
          <label>Рабочее окно UTC<input name="operating_hours_utc"
            placeholder="06:00-23:00" maxLength={11} defaultValue={profile?.operating_hours_utc ?? ''} /></label>
          <label>Curfew UTC<input name="curfew_utc"
            placeholder="23:00-06:00" maxLength={11} defaultValue={profile?.curfew_utc ?? ''} /></label>
        </div>
        <label>Комментарий<textarea name="comment" rows={2} maxLength={500} defaultValue={profile?.comment ?? ''} /></label>
        <ActionError error={action.error} />
        <Button type="submit" className="justify-self-start">{action.busy ? 'Сохранение…' : 'Сохранить профиль'}</Button>
      </fieldset>
    </form>
  </Sheet>
}

function RatesDesk({ api, editRates }: { api: OpsApi; editRates: boolean }) {
  const [savedData, setSavedData] = useState<CharterRateCard | null>(null)
  const [edit, setEdit] = useState<{ kind: 'rate'; value: CharterRate | null } | { kind: 'airport'; value: CharterAirportProfile | null }>()
  const close = () => setEdit(undefined)
  function saved(updated: CharterRateCard) {
    setSavedData(updated)
    close()
  }

  return <Resource load={api.charterRates} label="тарифы">
    {(snapshot, refresh, refreshing) => {
      const data = savedData ?? snapshot
      const canEdit = data.can_edit && editRates
      return <div className="space-y-3">
        <section className="ops-panel">
          <div className="panel-bar flex-wrap">
            <h2>Активные ставки</h2>
            <div className="flex items-center gap-1">
              {canEdit && <Button type="button" variant="outline" onClick={() => setEdit({ kind: 'rate', value: null })}><Plus size={16} />Новая ставка</Button>}
              <Button type="button" variant="ghost" size="icon" aria-label="Обновить тарифы" disabled={refreshing}
                onClick={() => { setSavedData(null); refresh() }}><RefreshCw size={16} /></Button>
            </div>
          </div>
          {!data.rates.length && <Empty>Активные ставки ещё не внесены.</Empty>}
          {data.rates.map((rate) => <article key={rate.id} className="border-b border-border last:border-b-0">
            <div className="flex flex-wrap items-center justify-between gap-2 px-3 pt-2">
              <h3 className="font-mono text-sm font-semibold">{rate.registration ?? rate.aircraft_type} <span className="minor">v{rate.version} · {rate.currency}</span></h3>
              {canEdit && <Button type="button" variant="outline" onClick={() => setEdit({ kind: 'rate', value: rate })}>Изменить</Button>}
            </div>
            <RateDetails rate={rate} />
          </article>)}
        </section>
        <section className="ops-panel">
          <div className="panel-bar flex-wrap">
            <h2>Профили сборов аэропортов</h2>
            {canEdit && <Button type="button" variant="outline" onClick={() => setEdit({ kind: 'airport', value: null })}><Plus size={16} />Новый профиль</Button>}
          </div>
          {!data.airport_profiles.length && <Empty>Профили сборов ещё не внесены.</Empty>}
          {data.airport_profiles.map((profile) => <article key={profile.scope_type + ':' + profile.scope_key} className="border-b border-border last:border-b-0">
            <div className="flex flex-wrap items-center justify-between gap-2 px-3 pt-2">
              <h3 className="font-mono text-sm font-semibold">{profile.scope_key}</h3>
              {canEdit && <Button type="button" variant="outline"
                onClick={() => setEdit({ kind: 'airport', value: profile })}>Изменить</Button>}
            </div>
            <Facts entries={[
              ['Handling / landing', money(profile.handling_landing_est, profile.currency)],
              ['Сбор с пассажира', money(profile.pax_fee_per_head, profile.currency)],
              ['Пролёт за час', money(profile.overflight_avg_per_hr, profile.currency)],
              ['Срок пермита', profile.permit_lead_days == null ? null : profile.permit_lead_days + ' дн.'],
              ['Рабочее окно UTC', profile.operating_hours_utc], ['Curfew UTC', profile.curfew_utc],
              ['Техстоп', duration(profile.techstop_turn_minutes)],
              ['Профиль действует с', quoteTime(profile.valid_from)],
              ...(profile.comment ? [['Комментарий', profile.comment] as [string, string]] : []),
            ]} />
          </article>)}
        </section>
        <section className="ops-panel">
          <div className="panel-bar"><h2>Архив версий ставок</h2></div>
          {!data.archive.length && <Empty>В архиве пока нет ставок.</Empty>}
          {data.archive.map((rate) => <details key={rate.id} className="tool-disclosure">
            <summary className="font-mono">{rate.registration ?? rate.aircraft_type} · v{rate.version} · {rate.currency}</summary>
            <RateDetails rate={rate} />
          </details>)}
        </section>
        {edit?.kind === 'rate' && canEdit && <RateEditor api={api} rate={edit.value} onClose={close} onSaved={saved} />}
        {edit?.kind === 'airport' && canEdit && <AirportProfileEditor api={api} profile={edit.value} onClose={close} onSaved={saved} />}
      </div>
    }}
  </Resource>
}

export function Charter({ api }: { api: OpsApi }) {
  return <Resource load={api.charterAccess} label="доступ к Charter Desk">
    {(access) => <div className="charter-desk">
      {access.sales_access ? <Tabs defaultValue="quote">
        <TabsList aria-label="Charter Desk">
          <TabsTrigger value="quote">QUOTE DESK</TabsTrigger>
          <TabsTrigger value="empty">EMPTY LEGS</TabsTrigger>
          <TabsTrigger value="rates">RATE CARD</TabsTrigger>
        </TabsList>
        <TabsContent value="quote" forceMount className="data-[state=inactive]:hidden"><QuoteDesk api={api} /></TabsContent>
        <TabsContent value="empty"><OpportunityDesk api={api} /></TabsContent>
        <TabsContent value="rates"><RatesDesk api={api} editRates={access.edit_rates} /></TabsContent>
      </Tabs> : <OpportunityDesk api={api} />}
    </div>}
  </Resource>
}
