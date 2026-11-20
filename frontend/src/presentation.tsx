import { RefreshCw } from 'lucide-react'
import { type ReactNode, useEffect, useState } from 'react'
import { ApiError, type Display, type Flight, type Load, type SourceState, type Status } from './api'
import { Badge, Button } from './components/ui'

export function utcTime(value: string | null) {
  return value ? new Date(value).toISOString().slice(11, 16) + 'Z' : '—'
}

export function utcDate(value: string) {
  return new Date(value.length === 10 ? value + 'T00:00:00Z' : value).toISOString().slice(0, 10)
}

export function utcStamp(value: string | null) {
  return value ? utcDate(value) + ' ' + utcTime(value) : 'NOT VERIFIED'
}

export function flightAirport(flight: Flight, side: 'departure' | 'arrival', mode: Display) {
  if (mode === 'UTC') return flight[side] ?? '—'
  const iata = side === 'departure' ? flight.departure_iata : flight.arrival_iata
  return iata ?? (flight[side] ? flight[side] + ' ICAO' : '—')
}

export function flightTime(flight: Flight, field: 'std' | 'sta', mode: Display) {
  const stamp = mode === 'LT' ? (field === 'std' ? flight.std_local : flight.sta_local) : flight[field]
  if (!stamp) return '—'
  const departure = mode === 'LT' ? flight.std_local : flight.std
  const reference = field === 'std' ? flight.std : departure
  const date = stamp.slice(0, 10) !== reference?.slice(0, 10) ? stamp.slice(5, 10) + ' ' : ''
  return date + (mode === 'LT' ? stamp.slice(11, 16) + 'L' : utcTime(stamp))
}

export function StatusBadge({ status, until }: { status: Status; until?: string | null }) {
  const [, repaint] = useState(0)
  const expires = until ? Date.parse(until) : Infinity
  useEffect(() => {
    const remaining = expires - Date.now()
    if (!Number.isFinite(remaining) || remaining <= 0 || status === 'STALE' || status === 'UNAVAILABLE') return
    const timer = window.setTimeout(() => repaint((value) => value + 1), remaining)
    return () => window.clearTimeout(timer)
  }, [expires, status])
  const shown = status !== 'UNAVAILABLE' && expires <= Date.now() ? 'STALE' : status
  return <Badge variant={shown === 'READY' ? 'accent' : shown === 'UNAVAILABLE' ? 'default' : 'warning'}>{shown}</Badge>
}

export function PanelHeader({ title, source, state }: { title: string; source: string; state: SourceState }) {
  return (
    <div className="panel-bar flex-wrap">
      <div><h2>{title}</h2><p className="minor">{source}{state.fetched_at && ' · ' + utcTime(state.fetched_at)}</p></div>
      <StatusBadge status={state.status} until={state.fresh_until} />
    </div>
  )
}

type Result<T> = { data: T; error: null } | { data: null; error: Error }

export function Resource<T>({
  load, label, pending, children, refreshKey = 0,
}: {
  load: Load<T>
  label: string
  pending?: ReactNode
  children: (data: T, refresh: () => void, refreshing: boolean) => ReactNode
  refreshKey?: number
}) {
  const [attempt, setAttempt] = useState(0)
  const requestId = attempt + ':' + refreshKey
  const [result, setResult] = useState<{ load: typeof load; id: string; value: Result<T> }>()
  useEffect(() => {
    const request = new AbortController()
    load(request.signal, attempt > 0 || refreshKey > 0).then(
      (data) => { if (!request.signal.aborted) setResult({ load, id: requestId, value: { data, error: null } }) },
      (error: Error) => { if (!request.signal.aborted) setResult({ load, id: requestId, value: { data: null, error } }) },
    )
    return () => request.abort()
  }, [load, attempt, refreshKey, requestId])

  const refreshing = result?.id !== requestId
  const value = result?.load === load && (!refreshing || !result.value.error) ? result.value : null
  const refresh = () => setAttempt((n) => n + 1)
  if (!value) {
    return <>{pending}<div className="read-state" role="status"><span className="loading-dot" />Loading {label}…</div></>
  }
  if (value.error) {
    const canRetry = !(value.error instanceof ApiError) || value.error.status >= 500
    return (
      <div className="read-state" role="alert">
        <p>{value.error instanceof ApiError ? value.error.message : 'Connection unavailable.'}</p>
        {canRetry && (
          <Button variant="outline" onClick={refresh}>
            <RefreshCw size={14} /> Retry
          </Button>
        )}
      </div>
    )
  }
  return children(value.data, refresh, refreshing)
}
