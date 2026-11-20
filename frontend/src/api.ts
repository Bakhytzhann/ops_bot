export interface Flight {
  id: number
  number: string | null
  departure: string | null
  arrival: string | null
  std: string
  sta: string | null
  aircraft: string | null
  aircraft_type: string | null
  cancelled: boolean
  ferry: boolean
}

interface Schedule {
  date: string
  complete: boolean
  flights: Flight[]
}

export interface FlightCard {
  flight: Flight
  ete_minutes: number | null
  route: string | null
  alternate: string | null
  plan_status: 'available' | 'partial' | 'unavailable'
  handling: { station: string; name: string; phone: string | null; email: string | null }[]
}

export interface CrewPreparation {
  available: boolean
  members: {
    name: string | null
    position: string | null
    duty: {
      start: string | null
      end: string | null
      reporting: boolean
      metrics: { name: string; actual: string | null; limit: string | null; offence: boolean }[]
    } | null
  }[]
}

export interface WeatherPreparation {
  partial: boolean
  fetched_at: string | null
  reports: { product: 'METAR' | 'TAF'; station: string; raw: string; sources: string[] }[]
}

export interface NotamPreparation {
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

export function createOpsApi(headers: HeadersInit) {
  async function get<T>(path: string, day: string, signal: AbortSignal): Promise<T> {
    const query = new URLSearchParams({ day })
    const response = await fetch(`${import.meta.env.BASE_URL}api/v1/${path}?${query}`, { headers, signal, cache: 'no-store' })
    if (!response.ok) {
      const body = await response.json().catch(() => null)
      throw new ApiError(typeof body?.detail === 'string' ? body.detail : 'OPS service unavailable.', response.status)
    }
    return response.json()
  }

  return {
    schedule: (day: string, signal: AbortSignal) => get<Schedule>('schedule', day, signal),
    card: (id: number, day: string, signal: AbortSignal) => get<FlightCard>(`flights/${id}`, day, signal),
    crew: (id: number, day: string, signal: AbortSignal) => get<CrewPreparation>(`flights/${id}/preparation/crew`, day, signal),
    weather: (id: number, day: string, signal: AbortSignal) => get<WeatherPreparation>(`flights/${id}/preparation/weather`, day, signal),
    notams: (id: number, day: string, signal: AbortSignal) => get<NotamPreparation>(`flights/${id}/preparation/notams`, day, signal),
  }
}

export type OpsApi = ReturnType<typeof createOpsApi>
