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
