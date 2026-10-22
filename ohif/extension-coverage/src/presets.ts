import type { WindowClass } from './types';

export interface Preset {
  id: string;
  label: string;
  short: string;
  key: string;
  ww: number;
  wc: number;
  cls: WindowClass | null;
}

/** Explicit W/L values (HU); keys 1-5 in the coverage mode. */
export const PRESETS: Record<string, Preset> = {
  soft: { id: 'soft', label: 'Soft tissue', short: 'Soft', key: '1', ww: 400, wc: 40, cls: 'soft' },
  lung: { id: 'lung', label: 'Lung', short: 'Lung', key: '2', ww: 1500, wc: -600, cls: 'lung' },
  bone: { id: 'bone', label: 'Bone', short: 'Bone', key: '3', ww: 2500, wc: 480, cls: 'bone' },
  liver: { id: 'liver', label: 'Liver', short: 'Liver', key: '4', ww: 150, wc: 90, cls: 'soft' },
  brain: { id: 'brain', label: 'Brain', short: 'Brain', key: '5', ww: 80, wc: 40, cls: null },
};

export const CLASS_PRESET: Record<WindowClass, Preset> = {
  soft: PRESETS.soft,
  lung: PRESETS.lung,
  bone: PRESETS.bone,
};

export const CLASS_BADGE: Record<WindowClass, { letter: string; color: string; title: string }> = {
  soft: { letter: 'S', color: '#c9a27e', title: 'needs soft-tissue window (e.g. 400/40)' },
  lung: { letter: 'L', color: '#7fb3e6', title: 'needs lung window (e.g. 1500/-600)' },
  bone: { letter: 'B', color: '#e3dccb', title: 'needs bone window (e.g. 2500/480)' },
};

/** Mirrors covmap/windows.py: the window class a displayed VOI qualifies for. */
export function classifyWindow(wc: number, ww: number): WindowClass | null {
  if (ww >= 800 && ww <= 2500 && wc >= -900 && wc <= -300) {
    return 'lung';
  }
  if (ww >= 1000 && ww <= 4500 && wc >= 150 && wc <= 1000) {
    return 'bone';
  }
  if (ww >= 100 && ww <= 700 && wc >= -50 && wc <= 150) {
    return 'soft';
  }
  return null;
}

/** The preset whose W/L the VOI shows exactly (within 0.5 HU), if any. */
export function presetOf(voi: { wc: number; ww: number } | null): Preset | null {
  if (!voi) {
    return null;
  }
  return (
    Object.values(PRESETS).find(p => Math.abs(p.wc - voi.wc) < 0.5 && Math.abs(p.ww - voi.ww) < 0.5) ?? null
  );
}

/** Mirrors covmap/scoring.py DEFAULTS (the server's rules for a new session). */
export const DEFAULT_RULES = { min_dwell_ms: 80, reviewed_threshold: 0.9, skipped_threshold: 0.05 };

/** The panel's label of a live reading. */
export const LIVE_LABEL = 'Live reading';

/** The panel's label for a session's stored label (the server's "live reading (OHIF)"). */
export function readingLabel(label: string | null | undefined): string {
  return !label || /^live reading/i.test(label) ? LIVE_LABEL : label;
}

/** Plain-language description of where a sample reading came from. */
export function sourceText(source: string | null | undefined): string {
  switch (source) {
    case 'browser-scripted':
      return 'scripted automated reading (not a human)';
    case 'simulated':
      return 'simulated reading (not a human)';
    case 'ohif':
      return 'reader in this viewer';
    default:
      return source ? `${source} (origin as recorded)` : 'unknown origin';
  }
}
