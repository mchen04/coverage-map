import type { Status } from '@covmap/organ-map';

export type { Status };
export type WindowClass = 'lung' | 'bone' | 'soft';
export type ReaderAction = 'done' | 'na' | 'flag' | 'clear';

export interface Organ {
  id: number;
  key: string;
  name: string;
  group: string;
  windows: WindowClass[];
  voxels: number;
  k_range: [number, number];
  center_k: number;
  center_sop: string;
  bbox_rc?: number[];
  centroid_lps: [number, number, number];
}

export interface Unseen {
  short_dwell: number;
  wrong_window: number;
  out_of_region: number;
  never_displayed: number;
}

export interface OrganCoverage {
  id: number;
  key: string;
  name: string;
  windows: WindowClass[];
  coverage: number;
  status: Status;
  seen_voxels: number;
  voxels: number;
  unseen: Unseen;
  reader?: 'done' | 'na' | null;
  flagged?: boolean;
  note?: string | null;
}

export interface Summary {
  reviewed: number;
  partial: number;
  skipped: number;
  organs: number;
  voxel_coverage: number;
  attested?: number;
  not_applicable?: number;
  flagged?: number;
  remaining?: string[];
  finished?: { confirmed: boolean; t_ms?: number; remaining?: string[]; at_finish?: AtFinish } | null;
}

/** What a confirmed finish confirmed (`summary.finished.at_finish`); kept if reading goes on. */
export interface AtFinish {
  reviewed: number;
  partial: number;
  skipped: number;
  voxel_coverage: number;
  statuses: Record<string, Status>;
}

/** One entry of `GET /api/sessions?case=` (the case's latest readings, newest first). */
export interface RecentReading {
  session: string;
  label: string | null;
  from_sample: string | null;
  created: string | null;
  events: number;
  last_t_ms: number | null;
  finished: boolean;
}

export interface Rules {
  min_dwell_ms: number;
  reviewed_threshold: number;
  skipped_threshold: number;
}

/** GET /api/sessions/{id}, and `session_info` in every coverage response. */
export interface SessionInfo {
  session: string;
  case: string;
  label?: string | null;
  from_sample?: string | null;
  sample_events?: number;
  created?: string;
  finished?: boolean;
  events?: number;
  last_seq: number | null;
  last_t_ms: number | null;
  rules?: Partial<Rules>;
  /** the last stack image of the case series the reader was shown, restored on resume */
  last_view?: StackView | null;
}

/** A displayed stack image of the case series: its SOP, case slice k and window. */
export interface StackView {
  sop: string;
  k: number;
  voi: { wc: number; ww: number };
  window: WindowClass | null;
}

export interface Coverage {
  case: string;
  session: string;
  provisional: boolean;
  params: Rules;
  organs: OrganCoverage[];
  summary: Summary;
  baseline?: Record<string, unknown>;
  events?: { total: number; views: number; ignored: Record<string, number>; sources: Record<string, number> };
  timing_ms?: Record<string, number>;
  session_info?: SessionInfo;
}

/** GET /api/sessions/{id}/gap?organ= : where the organ still has unseen voxels (case k). */
export interface Gap {
  organ: string;
  k: number | null;
  sop: string | null;
  window: WindowClass | null;
  unseen_voxels: number;
  ranges: [number, number][];
}

export interface CaseInfo {
  case: string;
  study_uid: string;
  series_uid: string;
  n_slices: number;
  organs: number;
  segmented: boolean;
}

/** Where the session came from, for the header and exports. */
export interface Origin {
  sample: { name: string; label: string; source: string; events?: number } | null;
  resumed: boolean;
}

export interface Volume {
  case: string;
  study_uid: string;
  series_uid: string;
  rows: number;
  cols: number;
  n_slices: number;
  sop_uids: string[];
  affine_lps: number[][];
}

export interface Sample {
  name: string;
  label: string;
  source: string;
  description?: string;
  created?: string;
  events?: number;
}

export interface TimelineFrame {
  t_ms: number;
  summary: Summary;
  statuses: Record<string, Status>;
  coverage?: Record<string, number>;
  /** the image the reader was on at this time (last stack view of the case series) */
  view?: StackView | null;
}

export interface ReaderLayer {
  reader: 'done' | 'na' | null;
  flagged: boolean;
  note: string | null;
}

export type CovEvent = Record<string, unknown> & {
  v: 1;
  seq: number;
  t_ms: number;
  kind: 'view' | 'hidden' | 'end' | 'annotation' | 'finish';
  source: 'ohif';
};

export interface ReplayState {
  frames: TimelineFrame[];
  /** the reader's marks (done / N/A / flag) at each frame's time, from the session's log */
  layers: Array<Record<string, ReaderLayer>> | null;
  index: number;
  playing: boolean;
}

export type Phase = 'idle' | 'waiting' | 'lookup' | 'session' | 'ready' | 'no-case' | 'error';

export interface CoverageState {
  phase: Phase;
  error: string | null;
  caseId: string | null;
  seriesUid: string | null;
  studyUid: string | null;
  sessionId: string | null;
  sessionLabel: string;
  sessionSource: string;
  organs: Organ[];
  coverage: Coverage | null;
  reader: Record<string, ReaderLayer>;
  selected: string | null;
  outline: boolean;
  finishOpen: boolean;
  replay: ReplayState | null;
  samples: Sample[];
  eventsSent: number;
  lastPostMs: number | null;
  /** displayed VOI of the active viewport (null until the first view) */
  voi: { wc: number; ww: number } | null;
  /** latest gap per organ (from the server) */
  gaps: Record<string, Gap>;
  origin: Origin;
  /** false while replay is open or a session is switching: nothing is recorded */
  recording: boolean;
  /** another tab of this browser records this session; this tab shows it but records nothing */
  otherTab: boolean;
}
