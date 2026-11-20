export type Status = 'READY' | 'PARTIAL' | 'UNAVAILABLE' | 'STALE'
export type Display = 'UTC' | 'LT'

export interface SourceState {
  status: Status
  fetched_at: string | null
  fresh_until: string | null
}

export interface Flight {
  id: number
  version: string
  number: string | null
  departure: string | null
  arrival: string | null
  std: string
  sta: string | null
  departure_iata: string | null
  arrival_iata: string | null
  std_local: string | null
  sta_local: string | null
  aircraft: string | null
  aircraft_type: string | null
  cancelled: boolean
  ferry: boolean
}

export type Selection = { flight: Flight; week: string }

interface Schedule extends SourceState {
  date: string
  end_date: string
  complete: boolean
  flights: Flight[]
}

export interface Plan extends SourceState {
  ete_minutes: number | null
  route: string | null
  alternate: string | null
  overflight: (string | null)[] | null
}

export interface Handling extends SourceState {
  handlers: { role: 'DEP' | 'ARR'; station: string; name: string; phone: string | null; email: string | null }[]
}

export interface CrewMember {
  name: string | null
  position: string | null
  role: 'CPT' | 'FO' | null
  hotel: string | null
  duty: {
    start: string | null
    end: string | null
    reporting: boolean
    duty_duration: string | null
    next_eobt: string | null
    fdp_remaining: string | null
    metrics: { name: string; actual: string | null; limit: string | null; limit_type: string | null; offence: boolean }[]
    violations: string[]
  } | null
}

export interface CrewPreparation extends SourceState {
  available: boolean
  members: CrewMember[]
}

export interface WeatherReport {
  product: 'METAR' | 'TAF'
  station: string
  raw: string
  sources: string[]
  issued_at: string | null
  valid_from: string | null
  valid_to: string | null
  status: Status
  fresh_until: string | null
  facts: Record<string, string>
}

export interface WeatherPreparation extends SourceState {
  partial: boolean
  stations: string[]
  reports: WeatherReport[]
}

export interface NotamPreparation extends SourceState {
  stations: string[]
  recognized: string[]
  notams: { id: string; station: string; status: 'affects' | 'review'; raw: string }[]
  briefing_url: string | null
  briefing_time: string | null
  departure_offset_minutes: number | null
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number) { super(message) }
}

export type Load<T> = (signal: AbortSignal, refresh?: boolean) => Promise<T>

export function createOpsApi(headers: HeadersInit) {
  const cache = new Map<string, { value: SourceState; until: number }>()

  async function get<T extends SourceState>(
    path: string, params: URLSearchParams, signal: AbortSignal, ttl: number, refresh: boolean,
  ): Promise<T> {
    const key = path + '?' + params
    const cached = cache.get(key)
    if (!refresh && cached && cached.until > Date.now()) return cached.value as T

    if (refresh) params.set('refresh', 'true')
    const response = await fetch(import.meta.env.BASE_URL + 'api/v1/' + path + '?' + params, { headers, signal, cache: 'no-store' })
    if (!response.ok) {
      const body = await response.json().catch(() => null)
      if (response.status === 401 || response.status === 403) cache.clear()
      throw new ApiError(typeof body?.detail === 'string' ? body.detail : 'OPS service unavailable.', response.status)
    }
    const value: T = await response.json()
    if (!signal.aborted) {
      const temporary = value.status === 'UNAVAILABLE' || value.status === 'STALE'
      const freshness = !temporary && value.fresh_until && path !== 'schedule' ? Date.parse(value.fresh_until) : Infinity
      const lifetime = temporary ? 5_000 : ttl
      cache.delete(key)
      cache.set(key, { value, until: Math.min(Date.now() + lifetime, freshness) })
      if (cache.size > 64) {
        const oldest = cache.keys().next().value
        if (oldest) cache.delete(oldest)
      }
    }
    return value
  }

  function section<T extends SourceState>(
    selection: Selection, path: string, signal: AbortSignal, refresh: boolean, ttl: number,
  ) {
    const { flight, week } = selection
    const params = new URLSearchParams({ day: flight.std.slice(0, 10), week, version: flight.version })
    if (path === 'crew-assignment') params.set('assignments_only', 'true')
    const endpoint = path === 'crew-assignment' ? 'preparation/crew' : path
    return get<T>('flights/' + flight.id + '/' + endpoint, params, signal, ttl, refresh)
  }

  return {
    schedule: (day: string, end: string, signal: AbortSignal, refresh = false) =>
      get<Schedule>('schedule', new URLSearchParams({ day, end_day: end }), signal, 240_000, refresh),
    plan: (target: Selection, signal: AbortSignal, refresh = false) =>
      section<Plan>(target, 'plan', signal, refresh, 120_000),
    handling: (target: Selection, signal: AbortSignal, refresh = false) =>
      section<Handling>(target, 'handling', signal, refresh, 50_000),
    assignments: (target: Selection, signal: AbortSignal, refresh = false) =>
      section<CrewPreparation>(target, 'crew-assignment', signal, refresh, 50_000),
    crew: (target: Selection, signal: AbortSignal, refresh = false) =>
      section<CrewPreparation>(target, 'preparation/crew', signal, refresh, 50_000),
    metar: (target: Selection, signal: AbortSignal, refresh = false) =>
      section<WeatherPreparation>(target, 'weather', signal, refresh, 110_000),
    weather: (target: Selection, signal: AbortSignal, refresh = false) =>
      section<WeatherPreparation>(target, 'preparation/weather', signal, refresh, 110_000),
    notams: (target: Selection, signal: AbortSignal, refresh = false) =>
      section<NotamPreparation>(target, 'preparation/notams', signal, refresh, 110_000),
  }
}

export type OpsApi = ReturnType<typeof createOpsApi>
