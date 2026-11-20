import { RefreshCw } from 'lucide-react'
import { type ReactNode, useEffect, useState } from 'react'
import { ApiError } from './api'
import { Button } from './components/ui'

export function utcTime(value: string | null) {
  return value ? new Date(value).toISOString().slice(11, 16) + 'Z' : '—'
}

export function utcDate(value: string) {
  return new Date(value.length === 10 ? value + 'T00:00:00Z' : value).toISOString().slice(0, 10)
}

type Result<T> = { data: T; error: null } | { data: null; error: Error }

export function Resource<T>({
  load, label, pending, children,
}: {
  load: (signal: AbortSignal) => Promise<T>
  label: string
  pending?: ReactNode
  children: (data: T) => ReactNode
}) {
  const [attempt, setAttempt] = useState(0)
  const [result, setResult] = useState<{ load: typeof load; attempt: number; value: Result<T> }>()
  useEffect(() => {
    const request = new AbortController()
    load(request.signal).then(
      (data) => { if (!request.signal.aborted) setResult({ load, attempt, value: { data, error: null } }) },
      (error: Error) => { if (!request.signal.aborted) setResult({ load, attempt, value: { data: null, error } }) },
    )
    return () => request.abort()
  }, [load, attempt])

  const value = result?.load === load && result.attempt === attempt ? result.value : null
  if (!value) {
    return <>{pending}<div className="read-state" role="status"><span className="loading-dot" />Loading {label}…</div></>
  }
  if (value.error) {
    const canRetry = !(value.error instanceof ApiError) || value.error.status >= 500
    return (
      <div className="read-state" role="alert">
        <p>{value.error instanceof ApiError ? value.error.message : 'Connection unavailable.'}</p>
        {canRetry && (
          <Button variant="outline" onClick={() => setAttempt((n) => n + 1)}>
            <RefreshCw size={14} /> Retry
          </Button>
        )}
      </div>
    )
  }
  return children(value.data)
}
