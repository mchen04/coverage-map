import { useEffect, useState } from 'react';
import type CoverageService from '../CoverageService';
import type { CoverageState, Organ, ReaderLayer, Status, Unseen } from '../types';

export interface Row {
  organ: Organ;
  status: Status;
  coverage: number | null;
  unseen: Unseen | null;
  layer: ReaderLayer;
}

export interface SummaryView {
  reviewed: number;
  partial: number;
  skipped: number;
  remaining: number;
  voxelPct: number | null;
  attested: number;
  na: number;
  flagged: number;
}

const NO_LAYER: ReaderLayer = { reader: null, flagged: false, note: null };

export function useCoverageState(service: CoverageService): CoverageState {
  const [state, setState] = useState<CoverageState>(service.getState());
  useEffect(() => {
    setState(service.getState());
    const { unsubscribe } = service.subscribe(service.EVENTS.STATE_CHANGED, s =>
      setState(s as CoverageState)
    );
    return unsubscribe;
  }, [service]);
  return state;
}

/** Rows in list order; statuses from the live coverage or the replay frame. */
export function buildRows(service: CoverageService, state: CoverageState): Row[] {
  const frame = state.replay ? state.replay.frames[state.replay.index] : null;
  const live = new Map((state.coverage?.organs ?? []).map(o => [o.key, o]));
  return service.orderedOrgans().map(organ => {
    const c = live.get(organ.key);
    if (frame) {
      return {
        organ,
        status: frame.statuses[organ.key] ?? 'skipped',
        coverage: frame.coverage?.[organ.key] ?? null,
        unseen: null,
        layer: NO_LAYER,
      };
    }
    return {
      organ,
      status: c?.status ?? 'skipped',
      coverage: c ? c.coverage : state.coverage ? 0 : null,
      unseen: c?.unseen ?? null,
      layer: state.reader[organ.key] ?? NO_LAYER,
    };
  });
}

export function summarize(rows: Row[], state: CoverageState): SummaryView {
  const frame = state.replay ? state.replay.frames[state.replay.index] : null;
  const count = (s: Status) => rows.filter(r => r.status === s).length;
  const remaining = rows.filter(
    r => r.status !== 'reviewed' && r.layer.reader !== 'done' && r.layer.reader !== 'na'
  ).length;
  const vox = frame ? frame.summary?.voxel_coverage : state.coverage?.summary?.voxel_coverage;
  return {
    reviewed: count('reviewed'),
    partial: count('partial'),
    skipped: count('skipped'),
    remaining,
    voxelPct: typeof vox === 'number' ? Math.round(vox * 100) : null,
    attested: rows.filter(r => r.layer.reader === 'done' && r.status !== 'reviewed').length,
    na: rows.filter(r => r.layer.reader === 'na').length,
    flagged: rows.filter(r => r.layer.flagged).length,
  };
}

const REASONS: Array<[keyof Unseen, string, string]> = [
  ['never_displayed', 'unseen', 'never displayed'],
  ['wrong_window', 'window', 'shown only in a non-qualifying window'],
  ['out_of_region', 'off-view', 'its slices were shown but this part was outside the view'],
  ['short_dwell', 'brief', 'shown too briefly'],
];

/** Dominant unseen reason as a short hint plus a full breakdown. */
export function unseenHint(row: Row): { short: string; full: string } | null {
  if (row.status === 'reviewed' || !row.unseen) {
    return null;
  }
  const parts = REASONS.map(([k, short, long]) => ({ v: row.unseen![k] ?? 0, short, long })).filter(
    p => p.v >= 0.005
  );
  if (!parts.length) {
    return null;
  }
  const top = parts.reduce((a, b) => (b.v > a.v ? b : a));
  return {
    short: top.short,
    full: parts
      .sort((a, b) => b.v - a.v)
      .map(p => `${Math.round(p.v * 100)}% ${p.long}`)
      .join(' · '),
  };
}

export function pct(v: number | null): string {
  return v === null ? '–' : `${Math.round(v * 100)}%`;
}

export const STATUS_WORD: Record<Status, string> = {
  reviewed: 'reviewed',
  partial: 'partial',
  skipped: 'skipped',
};
