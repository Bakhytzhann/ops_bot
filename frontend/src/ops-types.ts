import type { SourceState } from './api'

export type CharterEstimateRequest = {
  departure: string; arrival: string; departure_utc: string; return_utc?: string | null
  aircraft: string; passengers: number; isa_deviation_c?: number; wind_kt?: number
}

export type CharterEstimateBasis = 'journey_log' | 'isa_model' | 'unavailable'

export type CharterEstimateLeg = {
  departure: string; arrival: string; departure_utc: string | null; arrival_utc: string | null
  route: string | null; distance_nm: number | null; distance_km: number | null
  block_minutes: number | null; fuel_kg: number | null
  block_basis: CharterEstimateBasis; fuel_basis: CharterEstimateBasis
  block_samples: number; fuel_samples: number; wind_kt: number
}

export type CharterScheduledFlight = {
  flight_id: number | null; flight_number: string | null; departure: string | null; arrival: string | null
  departure_utc: string; arrival_utc: string; ferry: boolean | null
}

type Rest = { actual: string | null; limit: string | null }
export type CharterCrew = {
  status: 'confirmation_required' | 'available' | 'unavailable'
  flight: { id: number; number: string | null; departure: string | null; arrival: string | null;
    departure_utc: string; arrival_utc: string } | null
  members: { role: string; fdp_remaining: string | null; next_eobt: string | null;
    rest_before: Rest | null; rest_after: Rest | null; violations: string[] }[]
}

export type CharterEstimateResponse = {
  aircraft: string; aircraft_type: string | null; passengers: number
  isa_deviation_c: number; wind_kt: number; legs: CharterEstimateLeg[]
  position: {
    status: 'at_departure' | 'ferry_required' | 'airborne' | 'unknown'; station: string | null
    as_of_utc: string; schedule_complete: boolean; ferry: CharterEstimateLeg | null
    scheduled_ferry: CharterScheduledFlight | null; schedule_conflict: CharterScheduledFlight | null
    next_flight: CharterScheduledFlight | null; last_flight: CharterScheduledFlight | null
  }
  crew: CharterCrew
}

export type CharterAccess = { sales_access: boolean; edit_rates: boolean }

type CharterTimeMode = 'STD' | 'STA' | 'WINDOW'
type CharterRequestLeg = {
  dep_icao: string; arr_icao: string; date_utc: string; local_date: string
  target_time_utc: string | null; target_time_lt: string | null; tz_offset: string | null
  time_mode: CharterTimeMode; window_start_utc: string; window_end_utc: string; via?: string[]
  timezone_basis?: 'arrival' | 'departure' | 'utc' | null
  window_clock_start?: string | null; window_clock_stop?: string | null
}
export type CharterRequest = {
  legs: CharterRequestLeg[]; pax: number; is_return: boolean; stay_days: number; focus: 'full' | 'crew'
}
export type CharterParse = { request: CharterRequest | null; clarification?: string | null }

export type CharterChainSegment = {
  departure: string; arrival: string; departure_utc: string | null; arrival_utc: string | null
  route: string | null; distance_nm: number | null; block_minutes: number | null; fuel_kg: number | null
  block_basis: string; fuel_basis: string
  block_samples?: number; fuel_samples?: number; airborne_minutes?: number | null
}
export type CharterChainLeg = CharterChainSegment & {
  time_mode?: CharterTimeMode; window_start_utc?: string | null; window_end_utc?: string | null
  techstops?: { airport: string; arrival_utc: string | null; departure_utc: string | null; ground_minutes: number | null }[]
  segments?: CharterChainSegment[]; issues?: string[]; elapsed_minutes?: number | null
  alternative_chains?: CharterChainLeg[]; direct_issues?: string[]; performance_status?: string
}
export type CharterAssessment = { status: string; issues: string[] }
export type CharterQuoteFtlMetrics = {
  required_duty_minutes: number | null; duty_limit_minutes: number | null; duty_margin_minutes: number | null
  required_fdp_minutes: number | null; fdp_limit_minutes: number | null; fdp_margin_minutes: number | null
}
export type CharterQuoteFtl = CharterAssessment & CharterQuoteFtlMetrics & {
  status: 'fits' | 'limited' | 'fits_after_rest' | 'unavailable' | 'within_current_limits' | 'partial'
  basis?: string | null; source?: string | null
  first_limited_segment: number | null; report_utc?: string | null; required_rest_minutes?: number | null
  available_from_utc?: string | null; periods?: (CharterQuoteFtlMetrics & {
    first_sector: number; last_sector: number; report_utc?: string | null; release_utc?: string | null
    fdp_end_utc?: string | null; required_rest_minutes?: number | null
  })[]
}
export type CharterCurrentCrewMember = {
  name: string | null; role: string | null; status: string
  duty_start_utc: string | null; duty_elapsed_seconds: number | null; duty_remaining_seconds: number | null
  duty_limit_utc: string | null; rest_until_utc: string | null; next_eobt_utc: string | null
  fdp_start_utc?: string | null; fdp_elapsed_seconds?: number | null; fdp_remaining_seconds?: number | null
  fdp_limit_utc?: string | null
}
export type CharterCurrentAircraft = {
  registration: string; aircraft_type: string | null
  position: { current_station: string | null; current_status: string }
  crew: {
    status: string; members: CharterCurrentCrewMember[]; source?: string | null; as_of_utc?: string | null
    reason?: string | null
    flight?: { number?: string | null; departure?: string | null; arrival?: string | null } | null
  }
}
export type CharterCurrentFleet = Partial<SourceState> & {
  aircraft?: CharterCurrentAircraft[]; as_of_utc?: string; schedule_complete?: boolean; complete?: boolean
}
export type CharterCrewScenario = {
  name?: string | null; role?: string | null; report_utc?: string | null; available_from_utc?: string | null
  duty_end_utc?: string | null; remaining_minutes?: number | null; status: string; issues?: string[]
  duty_at_report_minutes?: number | null; fdp_at_report_minutes?: number | null
  fdp_end_utc?: string | null; first_limited_segment?: number | null; earliest_continuation_utc?: string | null
  handover_station?: string | null; handover_utc?: string | null; contact?: string | null
  positioning?: {
    departure: string; arrival: string; departure_utc: string; arrival_utc: string; ready_utc: string
    flight_number: string; counted_duty_minutes: number; counted_fdp_minutes: number
  } | null
  rest_breaks?: { station: string; previous_release_utc: string; available_from_utc: string; next_departure_utc: string }[]
  members?: {
    name?: string | null; role?: string | null; contact?: string | null
    fdp_remaining?: string | null; next_eobt?: string | null; rest_before?: Rest | null; rest_after?: Rest | null
    violations?: string[]
  }[]
}
export type CharterAirportCheck = CharterAssessment & {
  airport: string; role: string; time_utc: string | null
  time_lt?: string | null; timezone?: string | null
  opening_hours_utc: string | null; curfew_utc: string | null; permit_lead_days: number | null
}

export type CharterEconomics = {
  currency: 'USD' | 'EUR' | null; pax_cost: number | null; ferry_cost: number | null
  wait_cost: number | null; fees_est: number | null; fuel_addon: number | null
  floor: number | null; target: number | null; rate_version: number | null; missing: string[]
}
export type CharterMatrixAircraft = {
  registration: string; aircraft_type: string | null
  position: {
    current_station: string | null; current_status: string; planned_station: string | null
    projected_status: string; as_of_utc: string | null; planned_for_utc?: string | null
  }
  legs: CharterChainLeg[]; ferry: CharterChainLeg[]
  schedule: CharterAssessment & { conflicts: CharterScheduledFlight[] }
  crew: CharterAssessment & {
    working: CharterCrewScenario[]; replacements: CharterCrewScenario[]; source?: string; as_of_utc?: string
    quote_ftl?: CharterQuoteFtl | null; current_quote_ftl?: CharterQuoteFtl | null
  }
  airports: CharterAirportCheck[]; economics?: CharterEconomics | null
  offer_brief?: string | null; offer_brief_utc?: string | null; ops_brief?: string | null
}
export type CharterMatrix = {
  aircraft: CharterMatrixAircraft[]; request: CharterRequest; as_of_utc: string; schedule_complete: boolean
}

export type CharterOpportunities = {
  empty_legs: {
    registration: string; departure: string; arrival: string; departure_utc: string; arrival_utc: string
    flight_number: string | null; aircraft_type?: string | null
  }[]
  open_windows: { registration: string; station: string; start_utc: string; end_utc: string; minutes: number }[]
  schedule_complete: boolean; as_of_utc: string
}

export type CharterRateDraft = {
  registration: string | null; aircraft_type: string | null; currency: 'USD' | 'EUR'
  pax_hour_rate: number; ferry_hour_rate: number; overnight_fee: number; daily_min_hours: number | null
  fuel_included: boolean; fuel_price_per_kg: number | null; margin_pct: number; comment: string | null
}
export type CharterRate = CharterRateDraft & {
  id: number; version: number; valid_from: string; valid_to: string | null; archived_at: string | null; updated_by: string
}
export type CharterAirportProfileDraft = {
  scope_type: 'category' | 'icao'; scope_key: string; currency: 'USD' | 'EUR' | null
  handling_landing_est: number | null; pax_fee_per_head: number | null; overflight_avg_per_hr: number | null
  permit_lead_days: number | null; curfew_utc: string | null; operating_hours_utc: string | null
  techstop_turn_minutes: number | null; comment: string | null
}
export type CharterAirportProfile = CharterAirportProfileDraft & {
  valid_from: string; valid_to: string | null
}
export type CharterRateCard = {
  rates: CharterRate[]; airport_profiles: CharterAirportProfile[]; archive: CharterRate[]; can_edit: boolean
}

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
