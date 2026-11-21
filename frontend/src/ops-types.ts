import type { SourceState } from './api'

export type LegRef = {
  id: number | null; number: string | null; departure: string | null; arrival: string | null
  std: string | null; sta: string | null
  ferry?: boolean
}

export type FleetAircraft = {
  registration: string; type: string; station: string | null
  active: LegRef | null; last: LegRef | null; next: LegRef | null
  notes: { text: string; observed_at: string; verified: boolean; pinned: boolean; older: boolean }[] | null
}

export type FleetBrief = SourceState & {
  fleet?: FleetAircraft[]; notes_access?: 'ready' | 'unbound' | 'unavailable'; sales_access?: boolean
}

export type Turn = {
  key: string; station: string; registration: string | null; aircraft_type: string | null
  status: 'active' | 'recent' | 'clarify'
  inbound_id: number | null; outbound_id: number | null
  inbound_number: string | null; outbound_number: string | null
  planned_in: string | null; planned_out: string | null
  outbound_arrival?: string | null
  blockers?: Blocker[]
  ready_at?: string | null
}

export function turnForFlight(turns: Turn[] | undefined, flightId: number) {
  return turns?.find((turn) => turn.outbound_id === flightId) ??
    turns?.find((turn) => turn.inbound_id === flightId)
}

export type GroundList = SourceState & { cards: Turn[] }
export type Process = 'fuel' | 'handling' | 'cabin' | 'pax' | 'documents' | 'other'

export type GroundEvent = {
  kind: string; event_at?: string | null; target_at?: string | null
  expected_at?: string | null; process?: Process | null; text?: string; critical?: boolean
}
export type GroundEntry = GroundEvent & { request_id: string }
export type GroundReport = { request_id: string; items: GroundEvent[] }
export type GroundPreview = GroundReport & { station_notes_allowed: boolean }
export type Blocker = { process: Process; reason?: string; expected_at?: string | null; opened_at: string }

export type GroundDetail = SourceState & {
  turn: Turn
  events: { id: number; kind: string; event_at: string; source: string; detail: {
    process?: Process; text?: string; reason?: string; stand?: string; critical?: boolean; expected_at?: string; target_at?: string
  } }[]
  timing: {
    target: string | null; target_source: string; baseline_in: string | null; baseline_out: string | null
    in: string | null; out: string | null; ready: string | null; forecast_ready: string | null
    ready_not_before: string | null; unknown_blockers: boolean; risk_minutes: number | null
    inbound_delta_minutes: number | null; departure_delta_minutes: number | null
    station_delta_minutes: number | null; after_ready_minutes: number | null
    blockers: Blocker[]
    movement_conflicts: Record<string, string>
  }
}

export type StationReport = SourceState & {
  station?: string; days?: number; turns?: number; events?: number; event_counts?: Record<string, number>
  metrics?: Record<string, { count: number; median_minutes: number | null; p80_minutes: number | null }>
  notes?: { date: string; text: string }[]; critical?: { date: string; text: string }[]
}

type Cohort = { station: string | null; aircraft_type: string | null }
export type GroundHistory = SourceState & {
  samples?: Record<string, Cohort & { count: number; median_minutes: number; p80_minutes: number }>
  blocker?: (Cohort & { process: Process; count: number; turn_count: number }) | null
}

export type Aircraft = { id: number; registration: string; type: string | null }
export type AircraftList = SourceState & { aircraft?: Aircraft[] }
export type Documents = SourceState & {
  documents?: { title: string | null; type: string | null; expiry: string | null;
    never_expires: boolean; files: { name: string | null; url: string }[] }[]
}
export type Operator = SourceState & { certificates?: { name: string | null; number: string | null; code: string | null }[] }
export type Checklist = SourceState & { count?: number; items?: { item: string; statuses: string[] }[] }

export type PlannedPeriod = '7d' | '1m' | '3m' | '6m' | '12m'
export type JourneyDays = 7 | 30 | 90 | 365
type Ranking = { label: string; value: number }[]
export type Planned = SourceState & {
  start?: string; end?: string; legs?: number; cancelled?: number; unassigned?: number; excluded?: number
  minutes?: number; known_durations?: number; average?: number | null; median?: number | null
  ferry_count?: number; nonferry_count?: number; unknown_ferry?: number; ferry_minutes?: number
  nonferry_minutes?: number; ferry_missing_duration?: number; idle_minutes?: number; idle_72h?: number
  received_windows?: number; total_windows?: number; complete_windows?: number
  tails?: Ranking; routes?: Ranking; destinations?: Ranking; ferry_routes?: Ranking; idle_by_tail?: Ranking
  hours_by_tail?: Record<string, number>; hours_by_month?: Record<string, number>; legs_by_month?: Record<string, number>
  idle_count?: number; overlap_count?: number; position_mismatch_count?: number; turn_count?: number
}

type Sample = { count: number; mean: number | null; median: number | null; total: number | null }
export type JourneyFilters = { departure: string; arrival: string; aircraft_type: string }
export type Priorities = SourceState & { priorities?: string[] }
export type Journey = SourceState & {
  start?: string; end?: string; covered_days?: number; last_synced_at?: string | null; source_failed?: boolean
  scheduled_count?: number; actual_count?: number; missing_log_count?: number; open_log_count?: number
  unknown_log_count?: number; cancelled_count?: number; reported_ferry_count?: number; reported_unknown_ferry_count?: number
  block_minutes?: Sample; planned_block_minutes?: Sample; block_delta_minutes?: Sample; airborne_minutes?: Sample
  distance_nm?: Sample; fuel?: Record<string, Sample>; fuel_uplift?: Record<string, Sample>
  routes?: { departure: string; arrival: string; count: number }[]
  airports?: { icao: string; count: number }[]; aircraft_types?: { type: string; count: number }[]
}

export type Requests = SourceState & { requests?: { id: number; label: string }[] }
type RfqLeg = { departure: string | null; arrival: string | null; departure_utc: string | null; pax: number | null }
type Variant = {
  id: number | null; aircraft: string | null; type: string | null; subcharter: boolean; status: string | null
  outdated: boolean; quote_price: number | null; quote_currency: string | null; pricing_currency: string | null
  calculated_cost: number | null; reported_margin_pct: number | null; legs: RfqLeg[]
}
export type RequestDetail = SourceState & {
  rfq?: { reference: string; status: string | null; created_at: string | null; legs: RfqLeg[];
    variants: Variant[]; variant_count: number }
  history?: { route: string; aircraft_type: string | null; summary: Journey }[] | null
  foreflight?: { route: string; departure_utc: string; aircraft: string | null; ete_minutes: number | null;
    fuel_to_destination: number | null; fuel_unit: string | null; has_warnings: boolean } | null
  checklists?: Record<string, { count: number; items: { item: string; statuses: string[] }[] } | null>
}
