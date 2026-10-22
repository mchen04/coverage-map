/**
 * Cornerstone3D reading instrumentation.
 *
 * Every viewport element gets an IMAGE_RENDERED listener (attached on ELEMENT_ENABLED and
 * for viewports that already exist). After each render the displayed state is read back
 * from the viewport — current image (stack) or camera plane (volume), the visible region
 * as the four canvas corners mapped through `viewport.canvasToWorld`, and the VOI — and
 * emitted once per distinct state. Cornerstone 5.x canvas coordinates are CSS pixels
 * (canvasToWorld multiplies by devicePixelRatio internally), so the corners are taken at
 * canvas.width/height divided by devicePixelRatio, which maps exactly to the drawing
 * buffer edges.
 */
import {
  cache,
  Enums,
  eventTarget,
  getEnabledElement,
  getRenderingEngines,
  metaData,
  utilities as csUtils,
} from '@cornerstonejs/core';

export interface ViewState {
  viewport: string;
  vp_type: 'stack' | 'volume';
  series: string | null;
  sop: string | null;
  study: string | null;
  corners_lps: number[][];
  normal: number[];
  voi: { wc: number; ww: number };
  canvas_px: [number, number];
}

type Listener = (evt: Event) => void;

const round = (v: number, d: number) => Math.round(v * d) / d;

export function sopOf(imageId: string | undefined | null): string | null {
  if (!imageId) {
    return null;
  }
  const m = metaData.get('sopCommonModule', imageId) as { sopInstanceUID?: string } | undefined;
  if (m?.sopInstanceUID) {
    return m.sopInstanceUID;
  }
  const match = /instances\/([^/]+)\/frames/.exec(imageId) ?? /objectUID=([^&]+)/.exec(imageId);
  return match ? decodeURIComponent(match[1]) : null;
}

export function seriesOf(imageId: string | undefined | null): { series: string | null; study: string | null } {
  if (!imageId) {
    return { series: null, study: null };
  }
  const m = metaData.get('generalSeriesModule', imageId) as
    | { seriesInstanceUID?: string; studyInstanceUID?: string }
    | undefined;
  if (m?.seriesInstanceUID) {
    return { series: m.seriesInstanceUID, study: m.studyInstanceUID ?? null };
  }
  const s = /studies\/([^/]+)\/series\/([^/]+)/.exec(imageId);
  return s
    ? { series: decodeURIComponent(s[2]), study: decodeURIComponent(s[1]) }
    : { series: null, study: null };
}

function legacyType(vp: any): string {
  return vp?.requestedType ?? vp?.type;
}

export function isStack(vp: any): boolean {
  return legacyType(vp) === Enums.ViewportType.STACK;
}

export function isPlanarVolume(vp: any): boolean {
  return legacyType(vp) === Enums.ViewportType.ORTHOGRAPHIC;
}

/** Image id of what the viewport shows now (the rendered image for stacks). */
export function displayedImageId(vp: any): string | null {
  if (isStack(vp)) {
    return vp.getCornerstoneImage?.()?.imageId ?? null;
  }
  try {
    return vp.getCurrentImageId?.() ?? null;
  } catch {
    return null;
  }
}

function firstImageId(vp: any): string | null {
  try {
    return vp.getImageIds?.()?.[0] ?? null;
  } catch {
    return null; // volume viewports throw until their volume is in the cache
  }
}

/**
 * A planar volume viewport whose volume is not fully loaded: its planes show partial pixel data.
 * CS3D 5.6.8 sets loadStatus.loaded once every frame was processed, a failed frame included, so
 * every frame must also have loaded (framesLoaded counts each frame once, on success).
 */
export function volumeLoading(vp: any): boolean {
  if (!isPlanarVolume(vp)) {
    return false;
  }
  try {
    const vol: any = cache.getVolume(vp.getVolumeId());
    if (!vol || (vol.loadStatus != null && !vol.loadStatus.loaded)) {
      return true;
    }
    return typeof vol.framesLoaded === 'number' && vol.framesLoaded < (vol.totalNumFrames ?? 0);
  } catch {
    return true;
  }
}

export function readViewState(vp: any): ViewState | null {
  const stack = isStack(vp);
  if (!stack && !isPlanarVolume(vp)) {
    return null;
  }
  const canvas: HTMLCanvasElement | undefined = vp.canvas;
  const dpr = window.devicePixelRatio || 1;
  const w = (canvas?.width ?? 0) / dpr;
  const h = (canvas?.height ?? 0) / dpr;
  if (!w || !h) {
    return null;
  }
  const imageId = displayedImageId(vp);
  if (stack && !imageId) {
    return null;
  }
  const seriesImageId = imageId ?? firstImageId(vp);
  if (!seriesImageId) {
    return null; // volume still loading: the next render is captured
  }
  const { series, study } = seriesOf(seriesImageId);

  const props = vp.getProperties?.();
  const range = props?.voiRange;
  if (!range || !Number.isFinite(range.lower) || !Number.isFinite(range.upper)) {
    return null;
  }
  const { windowWidth, windowCenter } = csUtils.windowLevel.toWindowLevel(range.lower, range.upper);

  const corners = [
    [0, 0],
    [w, 0],
    [w, h],
    [0, h],
  ].map(p => {
    const world = vp.canvasToWorld(p);
    return [round(world[0], 1000), round(world[1], 1000), round(world[2], 1000)];
  });
  if (corners.some(c => c.some(v => !Number.isFinite(v)))) {
    return null;
  }
  const n = vp.getCamera()?.viewPlaneNormal ?? [0, 0, 1];
  return {
    viewport: vp.id,
    vp_type: stack ? 'stack' : 'volume',
    series,
    sop: stack ? sopOf(imageId) : null,
    study,
    corners_lps: corners,
    normal: [round(n[0], 1e5), round(n[1], 1e5), round(n[2], 1e5)],
    voi: { wc: round(windowCenter, 10), ww: round(windowWidth, 10) },
    canvas_px: [Math.round(w), Math.round(h)],
  };
}

function signature(s: ViewState): string {
  const c = s.corners_lps.map(p => p.map(v => v.toFixed(2)).join(',')).join(';');
  return `${s.sop}|${s.series}|${c}|${s.normal.map(v => v.toFixed(4)).join(',')}|${s.voi.wc},${s.voi.ww}|${s.canvas_px}`;
}

export function getViewport(viewportId: string): any {
  for (const engine of getRenderingEngines() ?? []) {
    const vp = engine.getViewport(viewportId);
    if (vp) {
      return vp;
    }
  }
  return null;
}

export function allViewports(): any[] {
  return (getRenderingEngines() ?? []).flatMap(e => e.getViewports());
}

/** Dedupe signature of a viewport whose volume is loading (hidden already sent). */
const LOADING = 'loading';

export class ViewTracker {
  private attached = new Map<string, { element: HTMLElement; listener: Listener }>();
  private last = new Map<string, string>();
  private running = false;

  constructor(
    private readonly onView: (s: ViewState) => void,
    private readonly onDisabled: (viewportId: string) => void,
    private readonly onRendered?: (viewportId: string) => void,
    private readonly onLoading?: (vp: any) => void
  ) {}

  start(): void {
    if (this.running) {
      return;
    }
    this.running = true;
    eventTarget.addEventListener(Enums.Events.ELEMENT_ENABLED, this.handleEnabled);
    eventTarget.addEventListener(Enums.Events.ELEMENT_DISABLED, this.handleDisabled);
    eventTarget.addEventListener(Enums.Events.IMAGE_VOLUME_LOADING_COMPLETED, this.handleVolumeLoaded);
    eventTarget.addEventListener(Enums.Events.IMAGE_VOLUME_MODIFIED, this.handleVolumeLoaded);
    allViewports().forEach(vp => this.attach(vp.element, vp.id));
  }

  stop(): void {
    if (!this.running) {
      return;
    }
    this.running = false;
    eventTarget.removeEventListener(Enums.Events.ELEMENT_ENABLED, this.handleEnabled);
    eventTarget.removeEventListener(Enums.Events.ELEMENT_DISABLED, this.handleDisabled);
    eventTarget.removeEventListener(Enums.Events.IMAGE_VOLUME_LOADING_COMPLETED, this.handleVolumeLoaded);
    eventTarget.removeEventListener(Enums.Events.IMAGE_VOLUME_MODIFIED, this.handleVolumeLoaded);
    [...this.attached.keys()].forEach(id => this.detach(id));
    this.last.clear();
  }

  /** Forget dedupe history so the current states are emitted again (after hidden/new session). */
  recaptureAll(): void {
    this.last.clear();
    this.attached.forEach((_, id) => this.capture(id));
  }

  forget(viewportId?: string): void {
    if (viewportId) {
      this.last.delete(viewportId);
    } else {
      this.last.clear();
    }
  }

  capture(viewportId: string): void {
    const entry = this.attached.get(viewportId);
    const vp = entry ? getEnabledElement(entry.element as HTMLDivElement)?.viewport : getViewport(viewportId);
    if (!vp) {
      return;
    }
    if (volumeLoading(vp)) {
      // no credit for planes of a volume still streaming in: close the viewport's previous
      // state once; the render after loading completes is captured
      this.onLoading?.(vp);
      if (this.last.get(viewportId) !== LOADING) {
        this.last.set(viewportId, LOADING);
        this.onDisabled(viewportId);
      }
      return;
    }
    let state: ViewState | null = null;
    try {
      state = readViewState(vp);
    } catch (e) {
      console.warn('covmap: could not read viewport state', e);
    }
    if (!state) {
      return;
    }
    const sig = signature(state);
    if (this.last.get(viewportId) === sig) {
      return;
    }
    this.last.set(viewportId, sig);
    this.onView(state);
  }

  private attach(element: HTMLElement, viewportId: string): void {
    if (!element || !viewportId) {
      return;
    }
    const existing = this.attached.get(viewportId);
    if (existing?.element === element) {
      return;
    }
    if (existing) {
      this.detach(viewportId);
    }
    const listener: Listener = () => {
      this.capture(viewportId);
      this.onRendered?.(viewportId);
    };
    element.addEventListener(Enums.Events.IMAGE_RENDERED, listener);
    this.attached.set(viewportId, { element, listener });
    requestAnimationFrame(() => this.capture(viewportId));
  }

  private detach(viewportId: string): void {
    const entry = this.attached.get(viewportId);
    if (entry) {
      entry.element.removeEventListener(Enums.Events.IMAGE_RENDERED, entry.listener);
      this.attached.delete(viewportId);
    }
    this.last.delete(viewportId);
  }

  private handleEnabled = (evt: Event) => {
    const { element, viewportId } = (evt as CustomEvent).detail ?? {};
    this.attach(element, viewportId);
  };

  /** A frame arrived or loading completed: viewports waiting for their volume are captured
   * once it has fully loaded (a frame retried after completion arrives as a modification). */
  private handleVolumeLoaded = () => {
    this.attached.forEach((_, id) => {
      if (this.last.get(id) === LOADING) {
        this.capture(id);
      }
    });
  };

  private handleDisabled = (evt: Event) => {
    const { viewportId } = (evt as CustomEvent).detail ?? {};
    if (viewportId && this.attached.has(viewportId)) {
      this.detach(viewportId);
      this.onDisabled(viewportId);
    }
  };
}
