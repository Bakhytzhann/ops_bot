import { ChevronRight, RefreshCw } from 'lucide-react'
import type { ReactNode } from 'react'
import type { SourceState } from './api'
import { Button } from './components/ui'
import { StatusBadge, utcTime } from './presentation'

export function ToolPanel({ title, source, state, refresh, refreshing, children }: {
  title: string; source: string; state: SourceState; refresh: () => void; refreshing: boolean; children: ReactNode
}) {
  return (
    <section className="ops-panel">
      <div className="panel-bar">
        <div><h2>{title}</h2><p className="minor">{source}{state.fetched_at && ' · ' + utcTime(state.fetched_at)}</p></div>
        <div className="flex shrink-0 items-center gap-1">
          <StatusBadge status={state.status} until={state.fresh_until} />
          <Button variant="ghost" size="icon" onClick={refresh} disabled={refreshing} aria-label={'Refresh ' + title}><RefreshCw size={16} /></Button>
        </div>
      </div>
      {state.status === 'UNAVAILABLE' ? <Empty>Source unavailable. Try refresh.</Empty> : children}
    </section>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="px-3 py-3 text-sm text-muted-foreground">{children}</p>
}

export function Facts({ entries }: { entries: [string, ReactNode][] }) {
  return (
    <dl className="facts">
      {entries.map(([label, value]) => (
        <div key={label}><dt>{label}</dt><dd>{value ?? '—'}</dd></div>
      ))}
    </dl>
  )
}

export function ToolTabs<T extends string>({ label, options, active, onChange }: {
  label: string; options: readonly (readonly [T, string])[]; active: T; onChange: (value: T) => void
}) {
  return (
    <div className="tool-tabs" role="group" aria-label={label}>
      {options.map(([value, text]) => (
        <Button key={value} variant={active === value ? 'default' : 'outline'} aria-pressed={active === value}
          onClick={() => onChange(value)}>{text}</Button>
      ))}
    </div>
  )
}

export function ToolRow({ title, detail, action, onClick }: {
  title: ReactNode; detail?: ReactNode; action?: ReactNode; onClick: () => void
}) {
  return (
    <button className="tool-row" type="button" onClick={onClick}>
      <span className="min-w-0"><strong className="block break-words">{title}</strong>{detail && <span className="minor block break-words">{detail}</span>}</span>
      <span className="flex shrink-0 items-center gap-2 text-muted-foreground">{action}<ChevronRight size={17} /></span>
    </button>
  )
}
