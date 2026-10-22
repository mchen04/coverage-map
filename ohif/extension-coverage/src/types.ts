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
  finished?: { confirmed: boolean; t_ms?: number; remaining?: string[] } | null;
}

export interface Rules {
  min_dwell_ms: number;
  reviewed_threshold: number;
  skipped_threshold: number;
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
  index: number;
  playing: boolean;
}

export type Phase = 'idle' | 'waiting' | 'lookup' | 'ready' | 'no-case' | 'error';

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
}
