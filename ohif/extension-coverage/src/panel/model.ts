import { useEffect, useState } from 'react';
import type CoverageService from '../CoverageService';
import type { Coverage, CoverageState, Organ, ReaderLayer, Status, Unseen } from '../types';

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

/** Rows in list order; statuses and reader marks from the live coverage or the replay frame. */
export function buildRows(service: CoverageService, state: CoverageState): Row[] {
  const frame = state.replay ? state.replay.frames[state.replay.index] : null;
  const marks = state.replay?.layers?.[state.replay.index] ?? null;
  const live = new Map((state.coverage?.organs ?? []).map(o => [o.key, o]));
  return service.orderedOrgans().map(organ => {
    const c = live.get(organ.key);
    if (frame) {
      return {
        organ,
        status: frame.statuses[organ.key] ?? 'skipped',
        coverage: frame.coverage?.[organ.key] ?? null,
        unseen: null,
        layer: marks?.[organ.key] ?? NO_LAYER,
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

/** Header counts. In replay they are the frame's own (the server's summary at that time,
 *  reader marks included); live they follow the rows (the reader's latest marks). */
export function summarize(rows: Row[], state: CoverageState): SummaryView {
  const frame = state.replay ? state.replay.frames[state.replay.index] : null;
  const fs = frame?.summary;
  const count = (s: Status) => rows.filter(r => r.status === s).length;
  const num = (v: unknown, fallback: number) => (typeof v === 'number' ? v : fallback);
  const remaining = rows.filter(
    r => r.status !== 'reviewed' && r.layer.reader !== 'done' && r.layer.reader !== 'na'
  ).length;
  const vox = frame ? fs?.voxel_coverage : state.coverage?.summary?.voxel_coverage;
  const attested = rows.filter(r => r.layer.reader === 'done' && r.status !== 'reviewed').length;
  const na = rows.filter(r => r.layer.reader === 'na').length;
  const flagged = rows.filter(r => r.layer.flagged).length;
  return {
    reviewed: count('reviewed'),
    partial: count('partial'),
    skipped: count('skipped'),
    remaining: fs && Array.isArray(fs.remaining) ? fs.remaining.length : remaining,
    voxelPct: typeof vox === 'number' ? floorPct(vox) : null,
    attested: fs ? num(fs.attested, attested) : attested,
    na: fs ? num(fs.not_applicable, na) : na,
    flagged: fs ? num(fs.flagged, flagged) : flagged,
  };
}

const REASONS: Array<[keyof Unseen, string, string]> = [
  ['never_displayed', 'never shown', 'never displayed'],
  ['wrong_window', 'wrong window', 'shown only in a non-qualifying window'],
  ['out_of_region', 'off-view', 'its slices were shown but this part was outside the view'],
  ['short_dwell', 'too brief', 'shown too briefly'],
];

/**
 * Dominant unseen reason as a short hint ("34% unseen · wrong window"; just the reason when
 * nothing counted yet) plus a full breakdown.
 */
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
  const shown = floorPct(row.coverage ?? 0);
  return {
    short: shown > 0 ? `${100 - shown}% unseen · ${top.short}` : top.short,
    full: parts
      .sort((a, b) => b.v - a.v)
      .map(p => `${Math.round(p.v * 100)}% ${p.long}`)
      .join(' · '),
  };
}

/**
 * After a confirmed finish the reader may read on (it is recorded): when the server's summary
 * no longer equals what the finish confirmed (`summary.finished.at_finish`), the voxel share
 * then and now, with as many decimals (up to 2) as it takes to tell them apart.
 */
export function finishDrift(cov: Coverage | null): { at: string; now: string; title: string } | null {
  const sm = cov?.summary;
  const at = sm?.finished?.confirmed ? sm.finished.at_finish : null;
  if (!sm || !at) {
    return null;
  }
  const statuses = Object.fromEntries((cov!.organs ?? []).map(o => [o.key, o.status]));
  const same =
    at.reviewed === sm.reviewed &&
    at.partial === sm.partial &&
    at.skipped === sm.skipped &&
    Math.abs(at.voxel_coverage - sm.voxel_coverage) < 1e-12 &&
    Object.entries(at.statuses ?? {}).every(([k, v]) => statuses[k] === undefined || statuses[k] === v);
  if (same) {
    return null;
  }
  const fmt = (v: number, d: number) => `${(Math.floor(v * 100 * 10 ** d + 1e-9) / 10 ** d).toFixed(d)}%`;
  let d = 0;
  while (d < 2 && fmt(at.voxel_coverage, d) === fmt(sm.voxel_coverage, d)) {
    d++;
  }
  const counts = (x: { reviewed: number; partial: number; skipped: number }) =>
    `${x.reviewed} reviewed, ${x.partial} partial, ${x.skipped} skipped`;
  return {
    at: fmt(at.voxel_coverage, d),
    now: fmt(sm.voxel_coverage, d),
    title:
      `Reading went on after the confirmed finish, and is recorded. At finish: ${counts(at)}, ` +
      `${fmt(at.voxel_coverage, d)} of voxels; now: ${counts(sm)}, ${fmt(sm.voxel_coverage, d)}. ` +
      'Exports keep what was confirmed in summary.finished.at_finish.',
  };
}

/** Whole percent rounded down, so a value under a threshold never displays as reaching it. */
export function floorPct(v: number): number {
  return Math.floor(v * 100 + 1e-9);
}

export function pct(v: number | null): string {
  return v === null ? '–' : `${floorPct(v)}%`;
}

/** "12–20, 25" from inclusive ranges, at most `max` ranges then "+n more". */
export function rangesText(ranges: Array<[number, number]>, max = 4): string {
  const parts = ranges.slice(0, max).map(([a, b]) => (a === b ? `${a}` : `${a}–${b}`));
  return parts.join(', ') + (ranges.length > max ? ` +${ranges.length - max} more` : '');
}

export const STATUS_WORD: Record<Status, string> = {
  reviewed: 'reviewed',
  partial: 'partial',
  skipped: 'skipped',
};
