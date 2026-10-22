import { PubSubService } from '@ohif/core';
import { Enums as csEnums, utilities as csUtils } from '@cornerstonejs/core';

import { CovmapApi, type ApiError } from './api';
import type { MeshesJson } from '@covmap/organ-map';
import {
  ViewTracker,
  allViewports,
  displayedImageId,
  getViewport,
  isPlanarVolume,
  isStack,
  seriesOf,
  sopOf,
  type ViewState,
} from './instrumentation';
import { OutlineOverlay, type Contours } from './outline';
import { CLASS_PRESET, LIVE_LABEL, PRESETS, presetOf, readingLabel, type Preset } from './presets';
import type {
  CaseInfo,
  CovEvent,
  Coverage,
  CoverageState,
  Gap,
  Organ,
  ReaderAction,
  ReaderLayer,
  RecentReading,
  Rules,
  Sample,
  SessionInfo,
  StackView,
  TimelineFrame,
  Volume,
  WindowClass,
} from './types';

type MeshData = { meta: MeshesJson; buffer: ArrayBuffer };

const POST_INTERVAL_MS = 400;
const MAX_QUEUE = 20000;
const GAP_TIMEOUT_MS = 2000;
const CLASS_ORDER: WindowClass[] = ['soft', 'lung', 'bone'];
const NO_READER: ReaderLayer = { reader: null, flagged: false, note: null };
/** localStorage: the last session per case, resumed after a reload (docs/SPEC.md UI contract). */
const STORE_PREFIX = 'covmap:session:';
/** Web Lock held by the one tab of this browser that records a session. */
const LOCK_PREFIX = 'covmap-session-';
/** While another tab records the session: how often this tab re-reads its coverage. */
const FOLLOW_MS = 2500;
const OTHER_TAB_HINT = 'This reading is open in another tab. Press "Record here" to record in this tab.';
/** Shown when a mark or Finish comes while New reading or Recent readings is switching sessions. */
const SWITCHING_HINT = 'The reading is changing. Try again in a moment.';
/** When the recording tab closes, the waiting tab takes over this long after the lock is
 *  free, so the closing tab's last batch (its pagehide beacon) is on the server first. */
const TAKEOVER_SETTLE_MS = 400;
/** Framing after a jump: margin around the organ, as a share of the canvas on each side. */
const FRAME_MARGIN = 0.08;
/** How long a jump waits for the organ's outline on the target slice before it uses bbox_rc. */
const OUTLINE_WAIT_MS = 1000;

interface StoredSession {
  session: string;
  label: string;
  sample: { name: string; label: string; source: string; events?: number } | null;
  saved: number;
}

interface SavedView {
  id: string;
  idx: number | null;
  camera: any;
  voi: { lower: number; upper: number } | null;
}

/** Display label of a sample without its own "Sample:" prefix. */
function sampleName(sample: { label?: string; name: string }): string {
  return (sample.label || sample.name).replace(/^sample\s*:?\s*/i, '');
}

/** The scorer's annotation rule (covmap/scoring.py Scorer._annotate) for one logged event. */
function logAction(layer: ReaderLayer, action: string, note?: string | null): ReaderLayer {
  if (action === 'clear') {
    return { ...NO_READER };
  }
  const next: ReaderLayer =
    action === 'flag' ? { ...layer, flagged: true } : { ...layer, reader: action as ReaderLayer['reader'] };
  return typeof note === 'string' ? { ...next, note: note || null } : next; // "" removes the note
}

/**
 * The reader's marks at each replay frame: the logged annotation events up to the frame's
 * time, in the scorer's order ((t_ms, seq); the first event of a seq counts).
 */
function frameLayers(
  frames: TimelineFrame[],
  log: Array<Record<string, any>>,
  organs: string[]
): Array<Record<string, ReaderLayer>> {
  const known = new Set(organs);
  const evs = log
    .filter(e => typeof e?.t_ms === 'number' && typeof e?.seq === 'number')
    .sort((a, b) => a.t_ms - b.t_ms || a.seq - b.seq);
  const used = new Set<number>();
  const cur: Record<string, ReaderLayer> = {};
  const out: Array<Record<string, ReaderLayer>> = [];
  let i = 0;
  for (const f of frames) {
    for (; i < evs.length && evs[i].t_ms <= f.t_ms; i++) {
      const e = evs[i];
      if (used.has(e.seq)) {
        continue;
      }
      used.add(e.seq);
      if (e.kind === 'annotation' && known.has(e.organ)) {
        cur[e.organ] = logAction(cur[e.organ] ?? NO_READER, e.action, e.note);
      }
    }
    out.push({ ...cur });
  }
  return out;
}

/** Marks a stack viewport's resetCamera as wrapped to restore the radiological flips. */
const RESET_HOOK = Symbol('covmap.radiologicalReset');

/** The stack a viewport shows (image count and first image), or null while it has none. */
function stackKey(vp: any): string | null {
  try {
    const ids: string[] = vp.getImageIds?.() ?? [];
    return `${ids.length}|${ids[0] ?? ''}`;
  } catch {
    return null;
  }
}

type Hooks = {
  sessionId: string | null;
  caseId: string | null;
  eventsSent: number;
  lastCoverage: Coverage | null;
  ready: boolean;
  firstImageRenderedAt: number | null;
  mapReadyAt: number | null;
  phase: string;
  flush: () => Promise<void>;
  state: () => CoverageState;
  /** Test hook: start a fresh session for this case without the confirm step (same path as
   *  the New reading button); resolves to the new session id once it is recording. */
  newReading: () => Promise<string | null>;
  [k: string]: unknown;
};

declare global {
  interface Window {
    __covmap?: Hooks;
  }
}

function initialState(): CoverageState {
  return {
    phase: 'idle',
    error: null,
    caseId: null,
    seriesUid: null,
    studyUid: null,
    sessionId: null,
    sessionLabel: '',
    sessionSource: 'ohif',
    organs: [],
    coverage: null,
    reader: {},
    selected: null,
    outline: false,
    finishOpen: false,
    replay: null,
    samples: [],
    eventsSent: 0,
    lastPostMs: null,
    voi: null,
    gaps: {},
    origin: { sample: null, resumed: false },
    recording: true,
    otherTab: false,
  };
}

function applyAction(layer: ReaderLayer, action: ReaderAction, note?: string | null): ReaderLayer {
  switch (action) {
    case 'done':
      return { ...layer, reader: 'done' };
    case 'na':
      return { ...layer, reader: 'na' };
    case 'flag':
      return { ...layer, flagged: true, note: typeof note === 'string' ? note || null : layer.note ?? null };
    default:
      return { ...NO_READER };
  }
}

export default class CoverageService extends PubSubService {
  public static readonly EVENTS = {
    STATE_CHANGED: 'event::covmap:stateChanged',
  };

  public static REGISTRATION = {
    name: 'coverageService',
    altName: 'CoverageService',
    create: ({ servicesManager, commandsManager }) =>
      new CoverageService(servicesManager, commandsManager),
  };

  public state: CoverageState = initialState();
  public api = new CovmapApi('/api');

  private tracker: ViewTracker;
  private queue: CovEvent[] = [];
  private seq = 0;
  /** session clock origin; set at the first event so pre-session events start near t=0 */
  private t0: number | null = null;
  private recording = true;
  private replaySaved: SavedView[] | null = null;
  private replayShown = -1;
  /**
   * stack viewports already checked for radiological orientation: per viewport instance, the
   * stack it was checked on and the flips that show that stack radiologically (null: as is)
   */
  private oriented = new WeakMap<object, { key: string; flips: { h: boolean; v: boolean } | null }>();
  /** volume viewports whose Reset View kept a preset, until the volume's late default window lands */
  private keptAfterReset = new WeakMap<object, Preset>();
  private radiological = true;
  private gapSeen = new Map<string, number>();
  private gapReq = new Map<string, Promise<Gap | null>>();
  private gapAt = 0;
  private jumpToken = 0;
  private order: { key: string; sopToIdx: Map<string, number>; kToNum: Map<number, number>; n: number } | null =
    null;
  private casesReq: Promise<CaseInfo[]> | null = null;
  private prevTitle: string | null = null;
  private timer: ReturnType<typeof setInterval> | null = null;
  private replayTimer: ReturnType<typeof setInterval> | null = null;
  private inflight: Promise<void> | null = null;
  private lookupTried = new Set<string>();
  private lookupBusy = false;
  private volume: Volume | null = null;
  private sopToK = new Map<string, number>();
  private pending = new Map<string, { layer: ReaderLayer; seq: number }>();
  private localReader: Record<string, ReaderLayer> = {};
  /** the last finish decision until the server has it (then the server's value is shown) */
  private localFinish: { confirmed: boolean; seq: number } | null = null;
  /**
   * Single writer per session: this tab records the session only while it holds the
   * session's Web Lock; another tab with the same session shows it without recording.
   */
  private writer = true;
  private lock: { session: string; token: number; release: () => void } | null = null;
  private lockToken = 0;
  /** a queued lock request: this tab takes the session over once the recording tab lets go */
  private lockWait: { session: string; ctl: AbortController } | null = null;
  /** the events of the POST in flight, unless sent with keepalive (sent again with the pagehide beacon) */
  private inflightEvents: CovEvent[] = [];
  private takingOver: Promise<boolean> | null = null;
  /** startReplay is loading the replay: recording stays paused, also after a takeover meanwhile. */
  private replayOpening = false;
  /** 409 resyncs since the last accepted batch (one retry; a second stops recording here) */
  private conflicts = 0;
  private followAt = -Infinity;
  private active = false;
  private switching = false;
  private outlineOverlay: OutlineOverlay;
  private firstImageAt: number | null = null;
  private meshCache = new Map<string, Promise<MeshData>>();
  private mapReadyAt: number | null = null;
  private renderedListeners = new Set<(viewportId: string) => void>();

  constructor(
    private readonly servicesManager: AppTypes.ServicesManager,
    private readonly commandsManager: AppTypes.CommandsManager
  ) {
    super(CoverageService.EVENTS);
    this.tracker = new ViewTracker(
      s => this.onView(s),
      id => this.enqueue('hidden', { viewport: id }),
      id => this.renderedListeners.forEach(cb => cb(id))
    );
    this.outlineOverlay = new OutlineOverlay(this);
  }

  // ---------------------------------------------------------------- lifecycle

  public start({ appConfig }: { appConfig?: Record<string, any> } = {}): void {
    if (this.active) {
      return;
    }
    this.active = true;
    this.api = new CovmapApi(appConfig?.covmap?.apiRoot ?? '/api');
    this.state = { ...initialState(), phase: 'waiting' };
    this.queue = [];
    this.seq = 0;
    this.t0 = null;
    this.recording = true;
    this.oriented = new WeakMap();
    this.order = null;
    this.lookupTried.clear();
    this.pending.clear();
    this.localReader = {};
    this.localFinish = null;
    this.writer = true;
    this.conflicts = 0;
    this.firstImageAt = null;
    this.mapReadyAt = null;
    document.addEventListener('visibilitychange', this.onVisibility);
    window.addEventListener('pagehide', this.onPageHide);
    window.addEventListener('pageshow', this.onPageShow);
    this.timer = setInterval(() => this.tick(), POST_INTERVAL_MS);
    this.prevTitle = document.title;
    document.title = 'Coverage Map';
    this.installHooks();
    this.tracker.start();
    this.outlineOverlay.start();
    this.emit();
  }

  public stop(): void {
    if (!this.active) {
      return;
    }
    // the lock goes once the last events are sent, so a tab taking over continues after them
    this.cancelWait();
    const lock = this.lock;
    this.lock = null;
    this.lockToken++;
    if (this.state.sessionId && this.writer) {
      this.enqueue('end', {});
      this.finalFlush().finally(() => lock?.release());
    } else {
      lock?.release();
    }
    this.tracker.stop();
    this.outlineOverlay.stop();
    this.stopReplay(false);
    if (this.prevTitle !== null) {
      document.title = this.prevTitle;
      this.prevTitle = null;
    }
    document.removeEventListener('visibilitychange', this.onVisibility);
    window.removeEventListener('pagehide', this.onPageHide);
    window.removeEventListener('pageshow', this.onPageShow);
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    this.active = false;
    this.state = initialState();
    this.emit();
  }

  public onModeExit(): void {
    this.stop();
  }

  public retry(): void {
    this.lookupTried.clear();
    Object.assign(this.state, { phase: this.state.caseId ? this.state.phase : 'waiting', error: null });
    this.emit();
    this.tracker.recaptureAll();
  }

  public getState(): CoverageState {
    return this.state;
  }

  public onViewportRendered(cb: (viewportId: string) => void): () => void {
    this.renderedListeners.add(cb);
    return () => this.renderedListeners.delete(cb);
  }

  public markMapReady(): void {
    if (this.mapReadyAt === null) {
      this.mapReadyAt = performance.now();
      performance.mark('covmap:map-ready');
    }
  }

  // ---------------------------------------------------------------- events

  private nowMs(): number {
    if (this.t0 === null) {
      return 0;
    }
    return Math.round((performance.now() - this.t0) * 10) / 10;
  }

  private setRecording(on: boolean): void {
    this.recording = on;
    if (this.state.recording !== on) {
      this.state.recording = on;
      this.emit();
    }
  }

  /** Display events (view, hidden) are not recorded while replay is open or a session is
   *  switching; reader events are, except while a session is switching (they would land in
   *  neither reading). A hidden tab displays nothing: no view is recorded until it is shown
   *  again (then the display is re-captured). Nothing is recorded while another tab records
   *  the session. */
  private enqueue(kind: CovEvent['kind'], fields: Record<string, unknown>): CovEvent | null {
    if (
      !this.writer ||
      (!this.recording && (kind === 'view' || kind === 'hidden')) ||
      (this.switching && (kind === 'annotation' || kind === 'finish')) ||
      (kind === 'view' && document.visibilityState === 'hidden')
    ) {
      return null;
    }
    if (this.t0 === null) {
      this.t0 = performance.now();
    }
    const ev = {
      v: 1,
      session: this.state.sessionId,
      case: this.state.caseId,
      seq: this.seq++,
      t_ms: this.nowMs(),
      kind,
      ...fields,
      source: 'ohif',
    } as CovEvent;
    this.queue.push(ev);
    if (this.queue.length > MAX_QUEUE) {
      this.queue.splice(0, this.queue.length - MAX_QUEUE);
    }
    return ev;
  }

  private onView(s: ViewState): void {
    if (this.firstImageAt === null) {
      this.firstImageAt = performance.now();
      performance.mark('covmap:first-image');
    }
    if (s.vp_type !== 'stack') {
      const vp = getViewport(s.viewport);
      if (vp) {
        this.hookReset(vp); // MPR and reoriented viewports: Reset View keeps a preset window too
        if (this.restoreKept(vp)) {
          return; // the preset is back: the next render is the displayed state
        }
      }
    } else if (this.orient(s)) {
      return; // flipped to radiological display: the next render is the displayed state
    }
    if (!this.state.caseId && s.series) {
      this.lookup(s.series, s.study);
    }
    this.trackVoi(s);
    const { study, ...fields } = s;
    this.enqueue('view', fields);
  }

  /** Remember the active viewport's displayed window for the preset buttons and hint. */
  private trackVoi(s: ViewState): void {
    const { viewportGridService } = this.servicesManager.services as any;
    const active = viewportGridService?.getActiveViewportId?.();
    if (active && s.viewport !== active) {
      return;
    }
    const cur = this.state.voi;
    if (!cur || cur.wc !== s.voi.wc || cur.ww !== s.voi.ww) {
      this.state.voi = { ...s.voi };
      this.emit();
    }
  }

  /**
   * Radiological display in coverage mode: axial stacks show anterior up and patient right
   * on screen left. Checked once per viewport and stack from the displayed corners (so it
   * holds whatever the acquisition direction); returns true when it flipped the viewport.
   * canvasToWorld follows the flip, so the recorded corners stay the true displayed region.
   */
  private orient(s: ViewState): boolean {
    const vp = getViewport(s.viewport);
    if (!vp || !isStack(vp)) {
      return false;
    }
    const key = stackKey(vp);
    if (key === null || this.oriented.get(vp)?.key === key) {
      return false;
    }
    this.hookReset(vp);
    const entry: { key: string; flips: { h: boolean; v: boolean } | null } = { key, flips: null };
    this.oriented.set(vp, entry);
    if (!this.radiological || Math.abs(s.normal[2]) < 0.9) {
      return false;
    }
    const [tl, tr, , bl] = s.corners_lps;
    const posteriorUp = tl[1] - bl[1] > 0; // screen up points to +y (posterior)
    const rightIsPatientRight = tr[0] - tl[0] < 0; // screen right points to -x (patient right)
    try {
      const before = vp.getCamera();
      if (posteriorUp) {
        vp.flip({ flipVertical: true });
      }
      if (rightIsPatientRight) {
        vp.flip({ flipHorizontal: true });
      }
      const after = vp.getCamera();
      const flipped =
        before.flipVertical !== after.flipVertical || before.flipHorizontal !== after.flipHorizontal;
      if ((posteriorUp || rightIsPatientRight) && !flipped) {
        this.oriented.delete(vp); // image data not ready: try again on the next render
        return false;
      }
      entry.flips = { h: !!after.flipHorizontal, v: !!after.flipVertical };
      return flipped;
    } catch (e) {
      console.warn('covmap: could not set radiological orientation', e);
      return false;
    }
  }

  /**
   * A camera reset (OHIF Reset View, viewport.resetCamera() from a script) clears the flips
   * that make an axial stack radiological. Cornerstone 5.6.8's StackViewport fires no
   * CAMERA_RESET event, so the viewport's own resetCamera is wrapped (once per instance; CS3D
   * assigns it per instance). The check runs as a microtask, before the next frame is drawn:
   * CS3D's internal resets (each new slice, resize, actor changes) restore the previous
   * presentation, flips included, synchronously, so only a plain reset is flipped back.
   *
   * OHIF's Reset View also calls resetProperties, which puts the image's own window back
   * (e.g. W2400/L176). That is wrapped too: a window that was one of the presets stays. Volume
   * viewports (MPR, reoriented) get the same wraps; reorient leaves them alone (no flips).
   */
  private hookReset(vp: any): void {
    const original = vp.resetCamera;
    if (typeof original === 'function' && !original[RESET_HOOK]) {
      const wrapped = (...args: unknown[]) => {
        const result = original.apply(vp, args);
        queueMicrotask(() => this.reorient(vp));
        return result;
      };
      (wrapped as any)[RESET_HOOK] = true;
      vp.resetCamera = wrapped;
    }
    const props = vp.resetProperties;
    if (typeof props === 'function' && !props[RESET_HOOK]) {
      const wrapped = (...args: unknown[]) => {
        const keep = this.presetShown(vp);
        const result = props.apply(vp, args);
        if (keep) {
          // set, not only shown: the preset also stays on the next slices
          vp.setProperties({ voiRange: csUtils.windowLevel.toLowHighRange(keep.ww, keep.wc) });
          if (!isStack(vp)) {
            // until the reader changes the window (the late default window fires no event)
            this.keptAfterReset.set(vp, keep);
            vp.element?.addEventListener(
              csEnums.Events.VOI_MODIFIED,
              () => this.keptAfterReset.delete(vp),
              { once: true }
            );
          }
        }
        return result;
      };
      (wrapped as any)[RESET_HOOK] = true;
      vp.resetProperties = wrapped;
    }
  }

  /**
   * A volume viewport's reset sets its default window later (CS3D's setDefaultVolumeVOI awaits
   * the range, then sets the transfer function without an event). Until the reader changes
   * the window, a render that shows no preset after a kept reset shows that default: the
   * preset goes back and that frame is not recorded. True when it put the preset back.
   */
  private restoreKept(vp: any): boolean {
    const kept = this.keptAfterReset.get(vp);
    if (!kept || this.presetShown(vp)) {
      return false;
    }
    this.keptAfterReset.delete(vp);
    vp.setProperties({ voiRange: csUtils.windowLevel.toLowHighRange(kept.ww, kept.wc) });
    vp.render();
    return true;
  }

  /** The preset a viewport's window shows exactly, if any. */
  private presetShown(vp: any): Preset | null {
    const r = vp.getProperties?.()?.voiRange;
    if (!r || !Number.isFinite(r.lower) || !Number.isFinite(r.upper)) {
      return null;
    }
    const { windowWidth, windowCenter } = csUtils.windowLevel.toWindowLevel(r.lower, r.upper);
    return presetOf({ wc: windowCenter, ww: windowWidth });
  }

  /** After a camera reset: apply the stack's radiological flips once more (flip never resets). */
  private reorient(vp: any): void {
    const entry = this.oriented.get(vp);
    if (!this.radiological || !entry?.flips || entry.key !== stackKey(vp)) {
      return;
    }
    try {
      const { h, v } = entry.flips;
      if (!!vp.getCamera().flipVertical !== v) {
        vp.flip({ flipVertical: true });
      }
      if (!!vp.getCamera().flipHorizontal !== h) {
        vp.flip({ flipHorizontal: true });
      }
      const cam = vp.getCamera();
      if (!!cam.flipVertical !== v || !!cam.flipHorizontal !== h) {
        this.oriented.delete(vp); // image data not ready: the next render checks the corners again
      }
    } catch (e) {
      console.warn('covmap: could not restore radiological orientation', e);
    }
  }

  /** Radiological display on (default) or the acquisition orientation (research/test hook). */
  public setRadiological(on: boolean): void {
    this.radiological = on;
    this.oriented = new WeakMap();
    for (const vp of allViewports()) {
      if (!isStack(vp) || on) {
        continue;
      }
      try {
        const cam = vp.getCamera();
        if (cam.flipVertical) {
          vp.flip({ flipVertical: true });
        }
        if (vp.getCamera().flipHorizontal) {
          vp.flip({ flipHorizontal: true });
        }
      } catch (e) {
        console.warn('covmap: could not reset orientation', e);
      }
    }
    this.tracker.recaptureAll();
  }

  private onVisibility = () => {
    if (document.visibilityState === 'hidden') {
      this.enqueue('hidden', { viewport: '*' });
      this.flush(true);
    } else {
      this.tracker.recaptureAll();
      if (!this.writer) {
        this.followAt = -Infinity; // shown again: the other tab's progress at once
        this.follow();
      }
    }
  };

  private onPageHide = () => {
    this.cancelWait();
    if (!this.state.sessionId) {
      return;
    }
    // 'end' is the session's last event: nothing displayed after it is recorded (the
    // visibilitychange that follows would otherwise post a 'hidden' racing the beacon)
    this.setRecording(false);
    if (this.writer) {
      this.enqueue('end', {});
      const sessionId = this.state.sessionId;
      // a POST in flight without keepalive may be cancelled with the page: its events go again,
      // before the queue and 'end' (the server scores an identical resend of a seq once), so
      // the beacon is complete and in order whichever request arrives first
      const seqs = new Set<number>();
      const events = [...this.inflightEvents, ...this.queue.splice(0)]
        .filter(e => !seqs.has(e.seq) && !!seqs.add(e.seq))
        .map(e => this.stamp(e));
      if (!this.api.beacon(sessionId, events, this.nowMs())) {
        this.api.postEvents(sessionId, events, this.nowMs(), true).catch(() => {});
      }
    }
    // another tab may record the session now (a page kept in the back/forward cache would
    // otherwise keep the lock)
    this.releaseLock();
  };

  /** Back from the back/forward cache: record again if no other tab took the session over
   *  (continuing after its last event), else show it without recording and wait for it. */
  private onPageShow = (e: PageTransitionEvent) => {
    if (!e.persisted || !this.state.sessionId || this.switching) {
      return;
    }
    if (this.writer) {
      this.takeOver(false);
    } else {
      this.waitForLock(this.state.sessionId);
    }
  };

  /** Send what is queued now (mode exit), after any request in flight, with keepalive. */
  private finalFlush(): Promise<unknown> {
    const sessionId = this.state.sessionId;
    if (!sessionId || !this.writer) {
      return Promise.resolve();
    }
    const events = this.queue.splice(0).map(e => this.stamp(e));
    const now = this.nowMs();
    const send = () =>
      this.api.postEvents(sessionId, events, now, true).catch(() => {
        this.api.beacon(sessionId, events, now);
      });
    return (this.inflight ?? Promise.resolve()).then(send, send);
  }

  private stamp(e: CovEvent): CovEvent {
    return { ...e, session: this.state.sessionId, case: this.state.caseId };
  }

  private tick(): void {
    if (this.state.phase !== 'ready' || !this.state.sessionId || this.switching) {
      return;
    }
    if (!this.writer) {
      this.follow();
    } else if (!this.inflight) {
      this.flush();
    }
  }

  /** While another tab records the session: show the server's coverage of it. */
  private follow(): void {
    const sessionId = this.state.sessionId;
    if (
      !sessionId ||
      this.state.replay ||
      document.visibilityState === 'hidden' ||
      performance.now() - this.followAt < FOLLOW_MS
    ) {
      return;
    }
    this.followAt = performance.now();
    this.api
      .coverage(sessionId)
      .then(cov => {
        if (sessionId === this.state.sessionId && !this.writer) {
          this.takeCoverage(cov);
        }
      })
      .catch(e => console.warn('covmap: coverage of the reading in the other tab unavailable', e));
  }

  /** POST queued events (or a heartbeat) and take the returned coverage. Nothing is sent
   *  while a session switch is in progress unless `force` (the old session's end), or while
   *  another tab records the session. A 409 (seq already used) resyncs and sends once more. */
  public async flush(keepalive = false, force = false): Promise<void> {
    while (this.inflight) {
      await this.inflight;
    }
    const sessionId = this.state.sessionId;
    if (!sessionId || !this.writer || (this.switching && !force)) {
      return;
    }
    const events = this.queue.splice(0).map(e => this.stamp(e));
    const maxSeq = events.length ? events[events.length - 1].seq : -1;
    const now = this.nowMs();
    const t = performance.now();
    let resend = false;
    // a keepalive POST outlives the page as the beacon does: the unload beacon resends only
    // the events of a POST that may be cancelled with the page
    this.inflightEvents = keepalive ? [] : events;
    this.inflight = (async () => {
      try {
        const cov = await this.api.postEvents(sessionId, events, now, keepalive);
        if (sessionId !== this.state.sessionId) {
          return;
        }
        this.conflicts = 0;
        this.state.eventsSent += events.length;
        this.state.lastPostMs = Math.round(performance.now() - t);
        this.pending.forEach((p, key) => p.seq <= maxSeq && this.pending.delete(key));
        if (this.localFinish && this.localFinish.seq <= maxSeq) {
          this.localFinish = null; // the server has the last finish decision now
        }
        this.takeCoverage(cov);
      } catch (e) {
        const err = e as ApiError;
        if (err.status === 400) {
          console.error('covmap: server rejected an event batch; dropped', e, events);
        } else if (err.status === 409) {
          resend = await this.resync(sessionId, events, err.body ?? null);
        } else {
          if (sessionId === this.state.sessionId && this.writer) {
            this.queue.unshift(...events);
          }
          console.warn('covmap: event POST failed, will retry', e);
        }
      }
    })().finally(() => {
      this.inflight = null;
      this.inflightEvents = [];
    });
    await this.inflight;
    if (resend) {
      await this.flush(keepalive, force);
    }
  }

  /**
   * 409: the server already has some of the batch's seqs from different events (another
   * writer on this session, e.g. a tab without Web Locks, or the last batch of the tab that
   * recorded before this one took over). Nothing of the batch was applied: continue after the
   * server's last event, renumbering the batch and everything queued since and keeping the
   * clock after its last t_ms, and send once more (true). A second conflict in a row means
   * another tab keeps recording: stop recording here and show the banner.
   */
  private async resync(
    sessionId: string,
    events: CovEvent[],
    body: Record<string, unknown> | null
  ): Promise<boolean> {
    if (sessionId !== this.state.sessionId || !this.writer) {
      console.info('covmap: batch refused (seq already used); this tab no longer records the session', body);
      return false;
    }
    if (this.conflicts > 0) {
      console.warn('covmap: seq conflict again after a resync: another writer records this session', body);
      this.releaseLock();
      this.standBy(false); // that writer holds no lock here: waiting for it would take it back at once
      return false;
    }
    this.conflicts++;
    const [bodySeq, bodyT] = [body?.last_seq, body?.last_t_ms];
    let lastSeq: number | null = typeof bodySeq === 'number' ? bodySeq : null;
    let lastT: number | null = typeof bodyT === 'number' ? bodyT : null;
    if (lastSeq === null) {
      try {
        const info = await this.api.session(sessionId);
        lastSeq = info.last_seq;
        lastT = info.last_t_ms;
      } catch (e) {
        console.warn('covmap: could not read the session after a seq conflict; will retry', e);
        this.queue.unshift(...events);
        return false;
      }
    }
    if (sessionId !== this.state.sessionId || !this.writer) {
      return false;
    }
    const batch = [...events, ...this.queue];
    const first = (lastSeq ?? -1) + 1;
    const shift = batch.length && lastT !== null && batch[0].t_ms <= lastT ? lastT + 1 - batch[0].t_ms : 0;
    const renumbered = new Map<number, number>();
    batch.forEach((e, i) => {
      renumbered.set(e.seq, first + i);
      e.seq = first + i;
      e.t_ms = Math.round((e.t_ms + shift) * 10) / 10;
    });
    this.seq = first + batch.length;
    if (shift > 0 && this.t0 !== null) {
      this.t0 -= shift;
    }
    this.pending.forEach(p => (p.seq = renumbered.get(p.seq) ?? p.seq));
    if (this.localFinish) {
      this.localFinish.seq = renumbered.get(this.localFinish.seq) ?? this.localFinish.seq;
    }
    this.queue = batch;
    console.warn(
      `covmap: seq conflict (409); continuing after the server's last event: ${batch.length} events ` +
        `renumbered from seq ${first}${shift > 0 ? `, clock +${Math.round(shift)} ms` : ''}`
    );
    return true;
  }

  // ---------------------------------------------------------------- single writer

  /** Take the session's Web Lock (`steal`: from the tab that holds it). True when held, or
   *  when the browser has no Web Locks (then every tab records, as before). */
  private acquireLock(sessionId: string, steal = false): Promise<boolean> {
    if (this.lock?.session === sessionId) {
      return Promise.resolve(true); // held already (a release would not be seen in time)
    }
    this.cancelWait();
    this.releaseLock();
    const locks = (navigator as any).locks;
    if (!locks?.request) {
      return Promise.resolve(true);
    }
    const token = ++this.lockToken;
    return new Promise<boolean>(resolve => {
      let answered = false;
      locks
        .request(LOCK_PREFIX + sessionId, steal ? { steal: true } : { ifAvailable: true }, (lock: unknown) => {
          answered = true;
          if (!lock || token !== this.lockToken) {
            resolve(false);
            return undefined;
          }
          // held until released here, or stolen by another tab (then request() rejects)
          return new Promise<void>(release => {
            this.lock = { session: sessionId, token, release };
            resolve(true);
          });
        })
        .catch((e: unknown) => {
          if (!answered) {
            // the lock manager refused the request itself: record without the guard
            console.warn('covmap: Web Locks unavailable; this tab records without the one-tab guard', e);
            resolve(true);
            return;
          }
          if (this.lock?.token === token) {
            this.lock = null;
            this.onLockLost(sessionId, e);
          }
        });
    });
  }

  private releaseLock(): void {
    const held = this.lock;
    this.lock = null;
    this.lockToken++;
    held?.release();
  }

  /**
   * Queue a request for the session's lock behind the tab that records it: when that tab
   * closes (or leaves the session) the lock is granted here and this tab takes the session
   * over by itself, continuing after the server's last event. Cancelled when this tab takes
   * or leaves the session (acquireLock, stop, pagehide).
   */
  private waitForLock(sessionId: string): void {
    const locks = (navigator as any).locks;
    if (!locks?.request || this.lockWait?.session === sessionId) {
      return;
    }
    this.cancelWait();
    const wait = { session: sessionId, ctl: new AbortController() };
    this.lockWait = wait;
    let token = -1;
    locks
      .request(LOCK_PREFIX + sessionId, { signal: wait.ctl.signal }, (lock: unknown) => {
        if (this.lockWait !== wait) {
          return undefined;
        }
        this.lockWait = null;
        if (!lock || sessionId !== this.state.sessionId || this.writer) {
          return undefined;
        }
        this.releaseLock();
        token = ++this.lockToken;
        // held until released here, or stolen by another tab (then request() rejects)
        return new Promise<void>(release => {
          this.lock = { session: sessionId, token, release };
          this.autoTakeOver(sessionId, token);
        });
      })
      .catch((e: unknown) => {
        if (this.lockWait === wait) {
          this.lockWait = null; // cancelled
        } else if (token >= 0 && this.lock?.token === token) {
          this.lock = null;
          this.onLockLost(sessionId, e);
        }
      });
  }

  private cancelWait(): void {
    const wait = this.lockWait;
    this.lockWait = null;
    wait?.ctl.abort();
  }

  /** The recording tab let the session go and this tab holds its lock now: record here. */
  private async autoTakeOver(sessionId: string, token: number): Promise<void> {
    await new Promise(r => setTimeout(r, TAKEOVER_SETTLE_MS));
    if (sessionId !== this.state.sessionId || this.writer || this.lock?.token !== token) {
      return;
    }
    console.info('covmap: the tab that recorded this reading let it go; recording here now');
    await this.takeOver(false);
  }

  /** Another tab pressed "Record here" for this session. */
  private onLockLost(sessionId: string, reason: unknown): void {
    if (sessionId !== this.state.sessionId || !this.writer) {
      return;
    }
    console.info('covmap: another tab records this reading now; not recording here', reason);
    this.standBy();
  }

  /** Another tab records this session: record nothing here, show the banner and follow the
   *  server's coverage; `wait`: take the session over once that tab lets it go. Whatever was
   *  queued here is dropped (it would collide with the other tab's numbering). */
  private standBy(wait = true): void {
    this.writer = false;
    this.queue = [];
    this.pending.clear();
    this.localReader = {};
    this.localFinish = null;
    this.conflicts = 0;
    this.followAt = -Infinity; // the next follow() reads at once
    Object.assign(this.state, { otherTab: true, finishOpen: false });
    this.state.reader = this.mergeReader(this.state.coverage);
    this.emit();
    this.follow();
    if (wait && this.state.sessionId) {
      this.waitForLock(this.state.sessionId);
    }
  }

  /**
   * Become the session's writer: take its lock (`steal`: from the other tab, which then
   * stops recording), then continue after the server's last event (seq and clock), as after
   * a reload. False (banner stays) when the lock is not free or the session is unreadable.
   */
  private takeOver(steal: boolean): Promise<boolean> {
    if (!this.takingOver) {
      this.takingOver = this.doTakeOver(steal).finally(() => {
        this.takingOver = null;
      });
    }
    return this.takingOver;
  }

  private async doTakeOver(steal: boolean): Promise<boolean> {
    const sessionId = this.state.sessionId;
    if (!sessionId) {
      return false;
    }
    const held = await this.acquireLock(sessionId, steal);
    if (sessionId !== this.state.sessionId) {
      return false;
    }
    if (!held) {
      this.standBy();
      return false;
    }
    let info: SessionInfo;
    try {
      info = await this.api.session(sessionId);
    } catch (e) {
      this.releaseLock();
      this.standBy(false); // not a lock wait: it would be granted at once and fail again
      this.notify(`Could not record here: ${(e as Error).message}`);
      return false;
    }
    if (sessionId !== this.state.sessionId) {
      return false;
    }
    this.continueAfter(info);
    this.pending.clear();
    this.localReader = {};
    this.localFinish = null;
    this.conflicts = 0;
    this.writer = true;
    this.state.otherTab = false;
    this.emit();
    // the other tab sent nothing when it let go: its display ends where it last reported,
    // also while the replay is open or opening here (closing the replay resumes recording)
    this.setRecording(true);
    this.enqueue('hidden', { viewport: '*' });
    if (this.state.replay || this.replayOpening) {
      this.setRecording(false);
    } else {
      this.resumeRecording();
    }
    await this.flush();
    return true;
  }

  /** "Record here": take the session over from the tab that records it. */
  public recordHere(): Promise<boolean> {
    if (this.writer || !this.state.sessionId) {
      return Promise.resolve(this.writer);
    }
    return this.takeOver(true);
  }

  /** Continue a stored session after the last time the server heard from it (its last event,
   *  or the last POST's now_ms if later: after Record here, the view the other tab showed
   *  until then stays open that long): anything queued is dropped and the displayed state is
   *  re-captured. */
  private continueAfter(info: SessionInfo): void {
    this.queue = [];
    this.seq = (info.last_seq ?? -1) + 1;
    this.t0 = performance.now() - (Math.max(info.last_t_ms ?? 0, info.last_now_ms ?? 0) + 1);
  }

  private notifyOtherTab(): void {
    this.notify(OTHER_TAB_HINT);
  }

  private takeCoverage(cov: Coverage): void {
    this.state.coverage = cov;
    this.state.reader = this.mergeReader(cov);
    this.emit();
    // keep "still to read" of the selected organ current while the reader works on it
    const key = this.state.selected;
    if (key && !this.state.replay) {
      const seen = cov.organs?.find(o => o.key === key)?.seen_voxels;
      if (seen !== undefined && seen !== this.gapSeen.get(key) && performance.now() - this.gapAt > 1200) {
        this.refreshGap(key);
      }
    }
  }

  private mergeReader(cov: Coverage | null): Record<string, ReaderLayer> {
    const out: Record<string, ReaderLayer> = {};
    const serverHasLayer = !!cov?.organs?.some(o => 'reader' in o || 'flagged' in o);
    for (const o of this.state.organs) {
      const server = cov?.organs?.find(c => c.key === o.key);
      const base: ReaderLayer =
        serverHasLayer && server
          ? { reader: server.reader ?? null, flagged: !!server.flagged, note: server.note ?? null }
          : (this.localReader[o.key] ?? NO_READER);
      out[o.key] = this.pending.get(o.key)?.layer ?? base;
    }
    return out;
  }

  // ---------------------------------------------------------------- case + session

  private async lookup(series: string, study: string | null): Promise<void> {
    if (this.lookupBusy || this.lookupTried.has(series)) {
      return;
    }
    this.lookupBusy = true;
    this.lookupTried.add(series);
    this.state.phase = 'lookup';
    this.emit();
    try {
      let caseId = await this.api.lookup({ series });
      if (!caseId && study) {
        caseId = await this.api.lookup({ study });
      }
      if (!caseId) {
        this.state.phase = 'no-case';
        this.state.error = 'This series is not a Coverage Map case (no segmentation).';
        this.emit();
        return;
      }
      await this.openCase(caseId);
    } catch (e) {
      this.state.phase = 'error';
      this.state.error = `Coverage API unavailable: ${(e as Error).message}`;
      this.lookupTried.delete(series);
      this.emit();
    } finally {
      this.lookupBusy = false;
    }
  }

  /** Organ meshes for the 3D map, fetched once per case. */
  public meshes(caseId: string): Promise<MeshData> {
    let p = this.meshCache.get(caseId);
    if (!p) {
      p = this.api.meshes(caseId);
      p.catch(() => this.meshCache.delete(caseId));
      this.meshCache.set(caseId, p);
    }
    return p;
  }

  private recall(caseId: string): StoredSession | null {
    try {
      const raw = localStorage.getItem(STORE_PREFIX + caseId);
      const rec = raw ? (JSON.parse(raw) as StoredSession) : null;
      return rec && typeof rec.session === 'string' ? rec : null;
    } catch {
      return null;
    }
  }

  private remember(caseId: string, rec: Omit<StoredSession, 'saved'>): void {
    try {
      localStorage.setItem(STORE_PREFIX + caseId, JSON.stringify({ ...rec, saved: Date.now() }));
    } catch {
      // storage unavailable (private mode): no resume after reload
    }
  }

  /** The case's last session if the server still has it, else a new live session; `writer`
   *  false when another tab of this browser records that session. */
  private async resumeOrCreate(
    caseId: string
  ): Promise<{ session: string; info: SessionInfo | null; rec: StoredSession | null; writer: boolean }> {
    const rec = this.recall(caseId);
    if (rec) {
      // the lock first, so the session read below is where this tab continues
      const writer = await this.acquireLock(rec.session);
      try {
        const info = await this.api.session(rec.session);
        if (info?.case === caseId) {
          return { session: rec.session, info, rec, writer };
        }
      } catch (e) {
        console.info('covmap: last session not resumable, starting a new one', e);
      }
      this.releaseLock();
    }
    const { session } = await this.api.createSession(caseId, 'live reading (OHIF)');
    const writer = await this.acquireLock(session);
    return { session, info: null, rec: null, writer };
  }

  private async openCase(caseId: string): Promise<void> {
    // Meshes, case data and the session load in parallel; the panel and map start as soon as
    // the case data is in, before the session exists.
    this.meshes(caseId).catch(() => {});
    const sessionReq = this.resumeOrCreate(caseId);
    sessionReq.catch(() => {});
    const [organs, volume, samples] = await Promise.all([
      this.api.organs(caseId),
      this.api.volume(caseId),
      this.api.samples(caseId).catch(() => [] as Sample[]),
    ]);
    this.volume = volume;
    this.sopToK = new Map(volume.sop_uids.map((s, k) => [s, k]));
    this.order = null;
    Object.assign(this.state, {
      caseId,
      seriesUid: volume.series_uid,
      studyUid: volume.study_uid,
      organs: organs.organs ?? [],
      samples: Array.isArray(samples) ? samples : [],
      error: null,
      phase: 'session',
    });
    this.emit();
    // open in a window that counts (soft tissue); the hotkeys 1-5 change it
    const vps = this.caseViewports();
    if (vps.length) {
      this.applyPreset(PRESETS.soft, vps);
    }
    const { session, info, rec, writer } = await sessionReq;
    // before anything is awaited: a tab without the lock sends nothing, not even a heartbeat
    this.writer = writer;
    if (info) {
      // Continue the stored session after its last event: events shown before it was found
      // (a few frames of this page load) are dropped and the current state re-captured.
      this.continueAfter(info);
    }
    const sample = rec?.sample ?? null;
    Object.assign(this.state, {
      sessionId: session,
      sessionLabel: rec?.label ?? LIVE_LABEL,
      sessionSource: 'ohif',
      origin: { sample, resumed: !!info },
      phase: 'ready',
    });
    if (!info) {
      this.remember(caseId, { session, label: LIVE_LABEL, sample: null });
    }
    this.state.reader = this.mergeReader(null);
    this.emit();
    if (info?.last_view) {
      // back where the reader left off (reload, or a case switch and back), not image 1 in Soft
      await this.showLastView(info.last_view);
    }
    if (!this.writer) {
      if (!this.takingOver) {
        this.standBy(); // another tab records this reading (and Record here was not pressed meanwhile)
      }
      return;
    }
    if (info) {
      this.tracker.recaptureAll();
    }
    this.flush();
  }

  /**
   * Resume: show the session's last displayed stack image in its window
   * (`session_info.last_view`). Not recorded while it moves; the caller re-captures the
   * restored display as the session's next view. Right after page load a scroll requested
   * during the first image load can still be pending (Cornerstone defers scroll() 40 ms for
   * an image not yet cached) and land on top of the restore, so the shown image is checked
   * and the restore repeated (a few times at most).
   */
  private async showLastView(view: StackView): Promise<void> {
    if (!view.voi || !this.caseViewports().length) {
      return;
    }
    const voiRange = csUtils.windowLevel.toLowHighRange(view.voi.ww, view.voi.wc);
    const wait = (ms: number) => new Promise(r => setTimeout(r, ms));
    const astray = () =>
      this.caseViewports().filter(
        vp =>
          isStack(vp) &&
          sopOf(displayedImageId(vp)) !== view.sop &&
          (vp.getImageIds() as string[]).some(id => sopOf(id) === view.sop)
      );
    const was = this.recording;
    this.setRecording(false);
    try {
      let targets = this.caseViewports();
      for (let attempt = 0; attempt < 4 && targets.length; attempt++) {
        for (const vp of targets) {
          // a superseded image load never settles: do not wait on it for long
          await Promise.race([this.showSlice(vp, view.sop, view.k, voiRange), wait(1500)]);
        }
        await wait(100);
        targets = astray();
      }
      if (targets.length) {
        console.warn('covmap: the last view could not be restored on', targets.map(vp => vp.id));
      }
    } catch (e) {
      console.warn('covmap: could not restore the last view', e);
    } finally {
      this.setRecording(was);
    }
  }

  private async restartSession(label: string, sample?: Sample): Promise<string | null> {
    const caseId = this.state.caseId;
    if (!caseId) {
      return null;
    }
    this.stopReplay(false);
    return this.openSession(caseId, label, sample);
  }

  /**
   * End the current session and start a new one. Until the new session exists nothing is
   * recorded or sent; anything queued meanwhile is dropped, so the new session's events start
   * at seq 0 and t_ms 0 (its view events are re-captured from what is displayed).
   */
  private async openSession(caseId: string, label: string, sample?: Sample): Promise<string> {
    this.switching = true;
    let session: string;
    try {
      this.setRecording(false);
      if (this.state.sessionId && this.writer) {
        this.enqueue('end', {});
        await this.flush(false, true);
      }
      this.pending.clear();
      this.localReader = {};
      this.localFinish = null;
      ({ session } = await this.api.createSession(
        caseId,
        sample ? label : 'live reading (OHIF)',
        sample?.name
      ));
      // the old session's lock goes, the new one's is free: this tab records the new session
      this.writer = await this.acquireLock(session);
      this.conflicts = 0;
      // Live events restart at t=0/seq=0; for sample sessions the server shifts them after
      // the prefilled sample events.
      this.queue = [];
      this.seq = 0;
      this.t0 = performance.now();
      const origin = sample
        ? { name: sample.name, label: sampleName(sample), source: sample.source, events: sample.events }
        : null;
      Object.assign(this.state, {
        sessionId: session,
        sessionLabel: label,
        sessionSource: 'ohif',
        origin: { sample: origin, resumed: false },
        otherTab: !this.writer,
        coverage: null,
        finishOpen: false,
        selected: null,
        outline: false,
        gaps: {},
        eventsSent: 0,
      });
      this.gapSeen.clear();
      this.remember(caseId, { session, label, sample: origin });
      this.state.reader = this.mergeReader(null);
      this.emit();
    } finally {
      this.switching = false;
      this.setRecording(true);
    }
    if (!this.writer) {
      this.waitForLock(session);
    }
    this.tracker.recaptureAll();
    await this.flush();
    return session;
  }

  /** Start a new live reading of this case (the caller confirms discarding the current one). */
  public newReading(): Promise<string | null> {
    return this.restartSession(LIVE_LABEL);
  }

  /** Start a new reading that begins with a sample's events; the reader continues on top. */
  public loadSample(name: string): Promise<string | null> {
    const sample = this.state.samples.find(s => s.name === name);
    if (!sample) {
      return Promise.resolve(null);
    }
    return this.restartSession(`Your reading, started from sample: ${sampleName(sample)}`, sample);
  }

  /** The case's latest readings on the server, newest first (this one included). */
  public async recentReadings(limit = 10): Promise<RecentReading[]> {
    const caseId = this.state.caseId;
    if (!caseId) {
      return [];
    }
    return (await this.api.recentSessions(caseId, limit)).sessions ?? [];
  }

  /**
   * Switch to an earlier reading of this case (Session menu, Recent readings): the current
   * session ends as on New reading, then the chosen one continues after its last event, as
   * after a reload (localStorage, last_view restored, seq and clock from the server). Another
   * tab recording it keeps it; this tab then shows it without recording until that tab lets go.
   */
  public async openReading(sessionId: string): Promise<string | null> {
    const caseId = this.state.caseId;
    if (!caseId || sessionId === this.state.sessionId) {
      return null;
    }
    // read it first: a reading the server does not have leaves the current one as it is
    let found: SessionInfo;
    try {
      found = await this.api.session(sessionId);
      if (found?.case !== caseId) {
        throw new Error(`it is not a reading of ${caseId}`);
      }
    } catch (e) {
      this.notify(`Could not open that reading: ${(e as Error).message}`);
      return null;
    }
    this.stopReplay(false);
    this.switching = true;
    let writer = false;
    try {
      this.setRecording(false);
      if (this.state.sessionId && this.writer) {
        this.enqueue('end', {});
        await this.flush(false, true);
      }
      this.pending.clear();
      this.localReader = {};
      this.localFinish = null;
      // the lock first, so the session read below is where this tab continues
      writer = await this.acquireLock(sessionId);
      const info = await this.api.session(sessionId).catch(() => found);
      this.writer = writer;
      this.conflicts = 0;
      this.continueAfter(info);
      const sample = info.from_sample ? this.state.samples.find(x => x.name === info.from_sample) : null;
      const origin = info.from_sample
        ? {
            name: info.from_sample,
            label: sample ? sampleName(sample) : info.from_sample,
            source: sample?.source ?? '',
            events: info.sample_events,
          }
        : null;
      const label = readingLabel(info.label);
      Object.assign(this.state, {
        sessionId,
        sessionLabel: label,
        sessionSource: 'ohif',
        origin: { sample: origin, resumed: true },
        otherTab: !writer,
        coverage: null,
        finishOpen: false,
        selected: null,
        outline: false,
        gaps: {},
        eventsSent: 0,
      });
      this.gapSeen.clear();
      this.remember(caseId, { session: sessionId, label, sample: origin });
      this.state.reader = this.mergeReader(null);
      this.emit();
      if (info.last_view) {
        await this.showLastView(info.last_view); // where that reading left off
      }
    } finally {
      this.switching = false;
      this.setRecording(true);
    }
    if (!this.writer) {
      if (!this.takingOver) {
        this.standBy(); // another tab records that reading (and Record here was not pressed meanwhile)
      }
      return sessionId;
    }
    this.tracker.recaptureAll();
    await this.flush();
    return sessionId;
  }

  /** True when discarding the session would lose something: coverage, reader marks, finish. */
  public hasReading(): boolean {
    const c = this.state.coverage;
    if (!this.state.sessionId) {
      return false;
    }
    if (this.localFinish || c?.summary?.finished || (c?.summary?.voxel_coverage ?? 0) > 0) {
      return true;
    }
    return Object.values(this.state.reader).some(l => !!l.reader || l.flagged);
  }

  /** Cases on this server (for the case picker), fetched once. */
  public listCases(): Promise<CaseInfo[]> {
    if (!this.casesReq) {
      this.casesReq = this.api.cases().catch(e => {
        this.casesReq = null;
        throw e;
      });
    }
    return this.casesReq;
  }

  public exportUrl(format: 'jsonl' | 'json' | 'csv'): string | null {
    return this.state.sessionId ? this.api.exportUrl(this.state.sessionId, format) : null;
  }

  public async applyRules(rules: Partial<Rules>): Promise<void> {
    if (this.state.replay) {
      // the menu disables Rules during replay; nothing fails silently
      this.notify('Close replay to change the reading rules.');
      throw new Error('Close replay to change the reading rules.');
    }
    if (!this.state.sessionId) {
      return;
    }
    await this.flush();
    const cov = await this.api.rules(this.state.sessionId, rules);
    this.takeCoverage(cov);
  }

  // ---------------------------------------------------------------- replay

  /**
   * Open the read-only replay. Recording pauses (a `hidden` event closes the live display
   * state, so nothing shown during the replay counts) and the viewer follows each frame's
   * image and window; closing restores the reader's slice and window and resumes recording.
   */
  public async startReplay(frames = 40): Promise<void> {
    const sessionId = this.state.sessionId;
    if (!sessionId || this.state.replay || !this.recording) {
      return;
    }
    this.enqueue('hidden', { viewport: '*' });
    const saved = this.saveView();
    this.setRecording(false);
    this.replayOpening = true;
    let list: TimelineFrame[] = [];
    let log: Array<Record<string, any>> | null = null;
    try {
      await this.flush();
      const [timeline, events] = await Promise.all([
        this.api.timeline(sessionId, frames),
        this.api.eventLog(sessionId).catch(e => {
          console.warn('covmap: event log unavailable; replay rows show no reader marks', e);
          return null;
        }),
      ]);
      list = timeline.frames ?? [];
      log = events;
    } catch (e) {
      console.warn('covmap: timeline failed', e);
    } finally {
      this.replayOpening = false;
    }
    if (!list.length || sessionId !== this.state.sessionId) {
      if (!list.length) {
        this.notify('Nothing to replay yet.');
      }
      this.resumeRecording();
      return;
    }
    this.replaySaved = saved;
    this.replayShown = -1;
    const layers = log ? frameLayers(list, log, this.state.organs.map(o => o.key)) : null;
    this.state.replay = { frames: list, layers, index: 0, playing: false };
    this.emit();
    this.showReplayFrame();
    this.playReplay();
  }

  /** Record again; the display is re-captured now, or when the tab is shown (a tab that
   *  took a session over while hidden records nothing until then). */
  private resumeRecording(): void {
    this.setRecording(true);
    if (document.visibilityState !== 'hidden') {
      this.tracker.recaptureAll();
    }
  }

  public setReplayIndex(index: number): void {
    const r = this.state.replay;
    if (!r) {
      return;
    }
    r.index = Math.max(0, Math.min(r.frames.length - 1, index));
    this.state.replay = { ...r };
    this.emit();
    this.showReplayFrame();
  }

  public playReplay(): void {
    const r = this.state.replay;
    if (!r) {
      return;
    }
    if (this.replayTimer) {
      clearInterval(this.replayTimer);
    }
    if (r.index >= r.frames.length - 1) {
      r.index = 0;
    }
    this.state.replay = { ...r, playing: true };
    this.emit();
    this.showReplayFrame();
    this.replayTimer = setInterval(() => {
      const cur = this.state.replay;
      if (!cur) {
        return;
      }
      if (cur.index >= cur.frames.length - 1) {
        this.pauseReplay();
        return;
      }
      this.state.replay = { ...cur, index: cur.index + 1 };
      this.emit();
      this.showReplayFrame();
    }, 250);
  }

  public pauseReplay(): void {
    if (this.replayTimer) {
      clearInterval(this.replayTimer);
      this.replayTimer = null;
    }
    if (this.state.replay) {
      this.state.replay = { ...this.state.replay, playing: false };
      this.emit();
    }
  }

  /** Close the replay; `restore` puts back the reader's view and resumes recording. */
  public stopReplay(restore = true): void {
    if (this.replayTimer) {
      clearInterval(this.replayTimer);
      this.replayTimer = null;
    }
    if (!this.state.replay) {
      return;
    }
    this.state.replay = null;
    this.emit();
    const saved = this.replaySaved;
    this.replaySaved = null;
    if (!restore) {
      return;
    }
    (async () => {
      try {
        if (saved) {
          await this.restoreView(saved);
        }
      } catch (e) {
        console.warn('covmap: could not restore the view after replay', e);
      } finally {
        this.resumeRecording();
      }
    })();
  }

  /** Drive the viewer to the replay frame's image and window (not recorded). */
  private showReplayFrame(): void {
    const r = this.state.replay;
    const view = r?.frames[r.index]?.view;
    if (!r || !view || this.replayShown === r.index) {
      return;
    }
    this.replayShown = r.index;
    const { lower, upper } = csUtils.windowLevel.toLowHighRange(view.voi.ww, view.voi.wc);
    for (const vp of this.caseViewports()) {
      this.showSlice(vp, view.sop, view.k, { lower, upper }).catch(e =>
        console.warn('covmap: replay view failed', e)
      );
    }
  }

  private saveView(): SavedView[] {
    return this.caseViewports().map(vp => {
      const stack = isStack(vp);
      const voi = vp.getProperties?.()?.voiRange;
      return {
        id: vp.id,
        idx: stack ? vp.getCurrentImageIdIndex() : null,
        camera: stack ? null : vp.getCamera(),
        voi: voi ? { lower: voi.lower, upper: voi.upper } : null,
      };
    });
  }

  private async restoreView(saved: SavedView[]): Promise<void> {
    for (const s of saved) {
      const vp = getViewport(s.id);
      if (!vp) {
        continue;
      }
      if (s.voi) {
        vp.setProperties({ voiRange: s.voi });
      }
      if (isStack(vp) && s.idx !== null) {
        if (s.idx !== vp.getCurrentImageIdIndex()) {
          await vp.setImageIdIndex(s.idx);
        } else {
          vp.render();
        }
      } else {
        if (s.camera) {
          vp.setCamera(s.camera);
        }
        vp.render();
      }
    }
  }

  // ---------------------------------------------------------------- organs, reader layer

  /** List order: by required window (soft, lung, bone) then head to feet. */
  public orderedOrgans(): Organ[] {
    const cls = (o: Organ) => CLASS_ORDER.indexOf(o.windows?.[0] ?? 'soft');
    return [...this.state.organs].sort(
      (a, b) => cls(a) - cls(b) || (b.centroid_lps?.[2] ?? 0) - (a.centroid_lps?.[2] ?? 0)
    );
  }

  public remaining(): string[] {
    const cov = this.state.coverage;
    const reader = this.state.reader;
    const byKey = new Map((cov?.organs ?? []).map(o => [o.key, o]));
    return this.orderedOrgans()
      .filter(o => {
        const r = reader[o.key]?.reader;
        return (byKey.get(o.key)?.status ?? 'skipped') !== 'reviewed' && r !== 'done' && r !== 'na';
      })
      .map(o => o.key);
  }

  /** The finish decision (docs/SPEC.md: finished once a finish was confirmed; backing out of
   *  a later Finish sheet does not undo it): this tab's own until the server has it, then the
   *  server's. */
  public finished(): { confirmed: boolean } | null {
    if (this.localFinish) {
      return { confirmed: this.localFinish.confirmed };
    }
    return this.state.coverage?.summary?.finished ?? null;
  }

  public select(key: string | null): void {
    this.state.selected = key;
    this.emit();
    if (key && !this.state.replay && !this.state.gaps[key]) {
      this.refreshGap(key);
    }
  }

  private annotate(key: string, action: ReaderAction, note?: string | null): void {
    if (this.state.replay) {
      this.notify('Replay is read-only. Close replay to annotate.');
      return;
    }
    if (!this.writer) {
      this.notifyOtherTab();
      return;
    }
    const fields: Record<string, unknown> = { organ: key, action };
    if (typeof note === 'string') {
      fields.note = note; // "" removes the note, no note keeps it
    }
    const ev = this.enqueue('annotation', fields);
    if (!ev) {
      return;
    }
    const prev = this.state.reader[key] ?? NO_READER;
    const layer = applyAction(prev, action, note);
    this.localReader[key] = applyAction(this.localReader[key] ?? NO_READER, action, note);
    this.pending.set(key, { layer, seq: ev.seq });
    this.state.reader = { ...this.state.reader, [key]: layer };
    this.emit();
  }

  private setLayer(key: string, next: ReaderLayer): void {
    if (!this.writer && !this.state.replay) {
      this.notifyOtherTab();
      return;
    }
    if (this.switching) {
      this.notify(SWITCHING_HINT);
      return;
    }
    const cur = this.state.reader[key] ?? NO_READER;
    if (cur.reader === next.reader && cur.flagged === next.flagged && cur.note === next.note) {
      return;
    }
    const needsClear =
      (cur.reader && cur.reader !== next.reader) || (cur.flagged && !next.flagged);
    if (needsClear) {
      this.annotate(key, 'clear');
    }
    const base = needsClear ? NO_READER : cur;
    if (next.reader && next.reader !== base.reader) {
      this.annotate(key, next.reader);
    }
    if (next.flagged && (!base.flagged || next.note !== base.note)) {
      this.annotate(key, 'flag', next.note ?? (base.note ? '' : null));
    }
    this.flush();
  }

  private targetKey(key?: string | null): string | null {
    const k = key ?? this.state.selected;
    if (!k) {
      this.notify('Select an organ first (click it in the list or the 3D map).');
    }
    return k;
  }

  public toggleDone(key?: string | null): void {
    const k = this.targetKey(key);
    if (!k) {
      return;
    }
    const cur = this.state.reader[k] ?? NO_READER;
    this.setLayer(k, { ...cur, reader: cur.reader === 'done' ? null : 'done' });
  }

  public toggleNA(key?: string | null): void {
    const k = this.targetKey(key);
    if (!k) {
      return;
    }
    const cur = this.state.reader[k] ?? NO_READER;
    this.setLayer(k, { ...cur, reader: cur.reader === 'na' ? null : 'na' });
  }

  public toggleFlag(key?: string | null, note?: string | null): void {
    const k = this.targetKey(key);
    if (!k) {
      return;
    }
    const cur = this.state.reader[k] ?? NO_READER;
    if (note !== undefined) {
      this.setLayer(k, { ...cur, flagged: true, note: note || null });
    } else {
      this.setLayer(k, { ...cur, flagged: !cur.flagged, note: cur.flagged ? null : cur.note });
    }
  }

  public toggleOutline(): void {
    if (!this.state.outline && !this.state.selected) {
      this.notify('Select an organ to outline.');
      return;
    }
    this.state.outline = !this.state.outline;
    this.emit();
  }

  public openFinish(open = !this.state.finishOpen): void {
    if (open && this.state.replay) {
      this.notify('Replay is read-only. Close replay first.');
      return;
    }
    if (open && !this.writer) {
      this.notifyOtherTab();
      return;
    }
    if (open && this.switching) {
      this.notify(SWITCHING_HINT);
      return;
    }
    this.state.finishOpen = open;
    this.emit();
  }

  /**
   * Record a finish decision and send it at once. Its t_ms is this moment; the POST that
   * carries it (right after any request in flight) has a fresh now_ms. A confirmed finish
   * closes every displayed view in the record at that t_ms (server), so what the screen showed
   * at Confirm is what the finish list, the exports and a reload show. Going back (Go back,
   * Escape) after a Confirm keeps the reading finished and records the display again, so
   * reading on counts.
   */
  public async finish(confirmed: boolean): Promise<void> {
    this.state.finishOpen = false;
    const wasConfirmed = !!this.finished()?.confirmed;
    const ev = this.enqueue('finish', { confirmed, remaining: this.remaining() });
    if (!ev) {
      this.emit();
      if (this.switching) {
        this.notify(SWITCHING_HINT);
      } else {
        this.notifyOtherTab();
      }
      return;
    }
    // shown at once; a confirmed finish stays (Go back after a Confirm does not undo it)
    this.localFinish = { confirmed: confirmed || wasConfirmed, seq: ev.seq };
    if (wasConfirmed && !confirmed) {
      this.tracker.recaptureAll();
    }
    this.emit();
    await this.flush();
  }

  // ---------------------------------------------------------------- navigation

  public sliceIndexOf(viewportId: string): number | null {
    const vp = getViewport(viewportId);
    const sop = vp ? sopOf(displayedImageId(vp)) : null;
    return sop ? (this.sopToK.get(sop) ?? null) : null;
  }

  public getVolume(): Volume | null {
    return this.volume;
  }

  /** Cornerstone viewports that currently show the case series. */
  public caseViewports(): any[] {
    const series = this.state.seriesUid;
    return allViewports().filter(vp => {
      if (!isStack(vp) && !isPlanarVolume(vp)) {
        return false;
      }
      let ids: string[] = [];
      try {
        ids = vp.getImageIds?.() ?? [];
      } catch {
        return false;
      }
      return !!series && ids.length > 0 && seriesOf(ids[0]).series === series;
    });
  }

  /** The stack order the viewer shows (image n of N), from a stack viewport of the case. */
  private viewerOrder(): { sopToIdx: Map<string, number>; kToNum: Map<number, number>; n: number } | null {
    const vp = this.caseViewports().find(v => isStack(v));
    if (!vp) {
      return this.order;
    }
    const ids: string[] = vp.getImageIds();
    const key = `${ids.length}|${ids[0]}|${ids[ids.length - 1]}`;
    if (this.order?.key !== key) {
      const sopToIdx = new Map<string, number>();
      const kToNum = new Map<number, number>();
      ids.forEach((id, i) => {
        const sop = sopOf(id);
        if (sop) {
          sopToIdx.set(sop, i);
          const k = this.sopToK.get(sop);
          if (k !== undefined) {
            kToNum.set(k, i + 1);
          }
        }
      });
      this.order = { key, sopToIdx, kToNum, n: ids.length };
    }
    return this.order;
  }

  /**
   * Case slice ranges (inclusive k) as the viewer numbers its images ("n" of "n/N"), merged
   * and sorted; null when no stack viewport shows the case series.
   */
  public viewerRanges(ranges: Array<[number, number]>): { ranges: Array<[number, number]>; n: number } | null {
    const order = this.viewerOrder();
    if (!order) {
      return null;
    }
    const out: Array<[number, number]> = [];
    for (const [k0, k1] of ranges) {
      const nums: number[] = [];
      for (let k = Math.min(k0, k1); k <= Math.max(k0, k1); k++) {
        const n = order.kToNum.get(k);
        if (n !== undefined) {
          nums.push(n);
        }
      }
      if (!nums.length) {
        continue;
      }
      nums.sort((a, b) => a - b);
      let start = nums[0];
      let prev = nums[0];
      for (const n of nums.slice(1)) {
        if (n !== prev + 1) {
          out.push([start, prev]);
          start = n;
        }
        prev = n;
      }
      out.push([start, prev]);
    }
    out.sort((a, b) => a[0] - b[0]);
    const merged: Array<[number, number]> = [];
    for (const r of out) {
      const last = merged[merged.length - 1];
      if (last && r[0] <= last[1] + 1) {
        last[1] = Math.max(last[1], r[1]);
      } else {
        merged.push([r[0], r[1]]);
      }
    }
    return { ranges: merged, n: order.n };
  }

  /** Where the organ still has unseen voxels (server gap endpoint); null if unavailable.
   *  A request already in flight for the organ is shared. */
  public refreshGap(key: string): Promise<Gap | null> {
    const sessionId = this.state.sessionId;
    if (!sessionId) {
      return Promise.resolve(null);
    }
    const inflight = this.gapReq.get(key);
    if (inflight) {
      return inflight;
    }
    this.gapAt = performance.now();
    const seen = this.state.coverage?.organs?.find(o => o.key === key)?.seen_voxels;
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), GAP_TIMEOUT_MS);
    const req = (async () => {
      try {
        // this tab's clock credits its open display state; not when another tab records
        const gap = await this.api.gap(sessionId, key, this.writer ? this.nowMs() : undefined, ctl.signal);
        if (sessionId !== this.state.sessionId) {
          return null;
        }
        if (seen !== undefined) {
          this.gapSeen.set(key, seen);
        }
        this.state.gaps = { ...this.state.gaps, [key]: gap };
        this.emit();
        return gap;
      } catch (e) {
        console.warn('covmap: gap lookup failed; using the organ centre', e);
        return null;
      } finally {
        clearTimeout(timer);
        this.gapReq.delete(key);
      }
    })();
    this.gapReq.set(key, req);
    return req;
  }

  /** Show case slice k (by its SOP on stacks) with a window, on one viewport. */
  private async showSlice(
    vp: any,
    sop: string | null,
    k: number,
    voiRange: { lower: number; upper: number },
    organ?: Organ
  ): Promise<void> {
    vp.setProperties({ voiRange });
    if (isStack(vp)) {
      const ids: string[] = vp.getImageIds();
      const want = sop ?? this.volume?.sop_uids[k];
      let idx = want ? (this.viewerOrder()?.sopToIdx.get(want) ?? -1) : -1;
      if (want && (idx < 0 || sopOf(ids[idx]) !== want)) {
        idx = ids.findIndex(id => sopOf(id) === want);
      }
      if (idx >= 0 && idx !== vp.getCurrentImageIdIndex()) {
        await vp.setImageIdIndex(idx);
      } else {
        vp.render();
      }
      return;
    }
    const A = this.volume?.affine_lps;
    if (A) {
      // the organ's in-plane centre on slice k (volume viewports: the plane through it)
      const bb = organ?.bbox_rc;
      const r = bb && bb.length >= 4 ? (bb[0] + bb[1]) / 2 : (this.volume!.rows - 1) / 2;
      const c = bb && bb.length >= 4 ? (bb[2] + bb[3]) / 2 : (this.volume!.cols - 1) / 2;
      const world = [0, 1, 2].map(i => A[i][0] * c + A[i][1] * r + A[i][2] * k + A[i][3]);
      vp.jumpToWorld(world);
    } else if (organ) {
      vp.jumpToWorld(organ.centroid_lps);
    }
    vp.render();
  }

  private activeViewport(): any {
    const { viewportGridService, cornerstoneViewportService } = this.servicesManager.services as any;
    const id = viewportGridService?.getActiveViewportId?.();
    return id ? cornerstoneViewportService?.getCornerstoneViewport?.(id) : null;
  }

  private async showCaseSeries(): Promise<any[]> {
    const { displaySetService, viewportGridService } = this.servicesManager.services as any;
    const ds = displaySetService
      ?.getActiveDisplaySets?.()
      ?.find(d => d.SeriesInstanceUID === this.state.seriesUid);
    const viewportId = viewportGridService?.getActiveViewportId?.();
    if (!ds || !viewportId) {
      return [];
    }
    viewportGridService.setDisplaySetsForViewport({
      viewportId,
      displaySetInstanceUIDs: [ds.displaySetInstanceUID],
    });
    for (let i = 0; i < 40; i++) {
      await new Promise(r => setTimeout(r, 100));
      const vps = this.caseViewports();
      if (vps.length) {
        return vps;
      }
    }
    return [];
  }

  public applyPreset(preset: Preset, viewports?: any[]): void {
    const targets = viewports ?? [this.activeViewport()].filter(Boolean);
    for (const vp of targets) {
      this.commandsManager.runCommand('setViewportWindowLevel', {
        viewportId: vp.id,
        windowWidth: preset.ww,
        windowCenter: preset.wc,
      });
    }
  }

  /**
   * The organ's in-plane extent on case slice k as image (r, c) bounds: its outline on that
   * slice (pixel edges), else its bbox_rc over all slices widened to pixel edges; null when
   * neither is known. The outline is waited for OUTLINE_WAIT_MS at most.
   */
  private async organExtent(organ: Organ, k: number): Promise<[number, number, number, number] | null> {
    const caseId = this.state.caseId;
    let contours: Contours | null = null;
    if (caseId) {
      const wait = new Promise<null>(r => setTimeout(() => r(null), OUTLINE_WAIT_MS));
      contours = await Promise.race([this.outlineOverlay.contours(caseId, organ.key, k).catch(() => null), wait]);
    }
    const pts = (contours ?? []).flat();
    if (pts.length) {
      const rs = pts.map(p => p[0]);
      const cs = pts.map(p => p[1]);
      return [Math.min(...rs), Math.max(...rs), Math.min(...cs), Math.max(...cs)];
    }
    const bb = organ.bbox_rc;
    return bb && bb.length >= 4 ? [bb[0] - 0.5, bb[1] + 0.5, bb[2] - 0.5, bb[3] + 0.5] : null;
  }

  /**
   * After a jump: when part of the organ's extent on slice k is outside the stack viewport's
   * canvas, pan to centre it and zoom out as far as needed to fit it with a margin (never
   * in). An organ fully in view leaves the reader's pan and zoom as they are. The camera only
   * moves in the image plane, so the radiological flips and the window stay. True if moved.
   */
  private frameOrgan(vp: any, extent: [number, number, number, number], k: number): boolean {
    const A = this.volume?.affine_lps;
    const canvas: HTMLCanvasElement | undefined = vp.canvas;
    const dpr = window.devicePixelRatio || 1;
    const w = (canvas?.width ?? 0) / dpr;
    const h = (canvas?.height ?? 0) / dpr;
    if (!A || !w || !h || !isStack(vp)) {
      return false;
    }
    try {
      const [r0, r1, c0, c1] = extent;
      // canvas positions do not depend on the slice shown (parallel projection, one plane normal)
      const px = [
        [r0, c0],
        [r0, c1],
        [r1, c0],
        [r1, c1],
      ].map(([r, c]) => vp.worldToCanvas([0, 1, 2].map(i => A[i][0] * c + A[i][1] * r + A[i][2] * k + A[i][3])));
      const xs = px.map(p => p[0]);
      const ys = px.map(p => p[1]);
      const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
      if (![x0, x1, y0, y1].every(Number.isFinite)) {
        return false;
      }
      if (x0 >= -0.5 && y0 >= -0.5 && x1 <= w + 0.5 && y1 <= h + 0.5) {
        return false; // fully in view: keep the reader's framing
      }
      const fit = 1 - 2 * FRAME_MARGIN;
      const zoomOut = Math.max(1, (x1 - x0) / (w * fit), (y1 - y0) / (h * fit));
      const cam = vp.getCamera();
      const to = vp.canvasToWorld([(x0 + x1) / 2, (y0 + y1) / 2]);
      const from = vp.canvasToWorld([w / 2, h / 2]);
      const d = [0, 1, 2].map(i => to[i] - from[i]);
      vp.setCamera({
        focalPoint: cam.focalPoint.map((v: number, i: number) => v + d[i]),
        position: cam.position.map((v: number, i: number) => v + d[i]),
        parallelScale: cam.parallelScale * zoomOut,
      });
      return true;
    } catch (e) {
      console.warn('covmap: could not frame the organ', e);
      return false;
    }
  }

  /**
   * Select an organ and show it: the slice where it still has the most unseen voxels (from
   * the server, after sending what is queued) in its window; the organ centre when nothing is
   * unseen or the gap lookup fails. A stack viewport whose pan or zoom leaves part of the
   * organ on that slice outside the canvas is reframed to show all of it (frameOrgan).
   */
  public async jumpToOrgan(key: string): Promise<void> {
    const organ = this.state.organs.find(o => o.key === key);
    if (!organ) {
      return;
    }
    const token = ++this.jumpToken;
    this.state.selected = key;
    this.emit();
    let sop: string | null = organ.center_sop ?? this.volume?.sop_uids[organ.center_k] ?? null;
    let k = organ.center_k;
    let cls: WindowClass = organ.windows?.[0] ?? 'soft';
    if (this.state.sessionId && !this.state.replay) {
      await this.flush();
      const gap = await this.refreshGap(key);
      if (token !== this.jumpToken) {
        return; // a newer jump replaced this one
      }
      if (gap && gap.k !== null && gap.k !== undefined) {
        k = gap.k;
        sop = gap.sop ?? this.volume?.sop_uids[gap.k] ?? null;
      }
      if (gap?.window && CLASS_PRESET[gap.window]) {
        cls = gap.window;
      }
    }
    const preset = CLASS_PRESET[cls] ?? PRESETS.soft;
    const extentReq = this.organExtent(organ, k);
    let targets = this.caseViewports();
    if (!targets.length) {
      targets = await this.showCaseSeries();
    }
    const extent = await extentReq;
    if (token !== this.jumpToken) {
      return;
    }
    const range = csUtils.windowLevel.toLowHighRange(preset.ww, preset.wc);
    for (const vp of targets) {
      try {
        // framed before the slice changes, so the reader sees one move; checked again after
        if (extent) {
          this.frameOrgan(vp, extent, k);
        }
        await this.showSlice(vp, sop, k, range, organ);
        if (extent && token === this.jumpToken && this.frameOrgan(vp, extent, k)) {
          vp.render();
        }
      } catch (e) {
        console.warn('covmap: jump failed', e);
      }
    }
  }

  public nextGap(): void {
    if (this.state.replay) {
      this.notify('Replay is read-only. Close replay first.');
      return;
    }
    if (!this.state.sessionId || !this.state.organs.length) {
      this.notify('Still loading the case: Next gap works once the organ list is shown.');
      return;
    }
    const remaining = this.remaining();
    if (!remaining.length) {
      this.notify('No gaps left: every organ is reviewed, done or N/A.');
      return;
    }
    const order = this.orderedOrgans().map(o => o.key);
    const start = this.state.selected ? order.indexOf(this.state.selected) : -1;
    for (let i = 1; i <= order.length; i++) {
      const key = order[(start + i + order.length) % order.length];
      if (remaining.includes(key)) {
        this.jumpToOrgan(key);
        return;
      }
    }
  }

  // ---------------------------------------------------------------- plumbing

  private notify(message: string): void {
    const { uiNotificationService } = this.servicesManager.services as any;
    uiNotificationService?.show?.({ title: 'Coverage Map', message, type: 'info', duration: 2500 });
  }

  private emit(): void {
    this.state = { ...this.state };
    this._broadcastEvent(CoverageService.EVENTS.STATE_CHANGED, this.state);
  }

  private installHooks(): void {
    const svc = this;
    window.__covmap = {
      get sessionId() {
        return svc.state.sessionId;
      },
      get caseId() {
        return svc.state.caseId;
      },
      get eventsSent() {
        return svc.state.eventsSent;
      },
      get lastCoverage() {
        return svc.state.coverage;
      },
      get ready() {
        return svc.state.phase === 'ready';
      },
      get phase() {
        return svc.state.phase;
      },
      get firstImageRenderedAt() {
        return svc.firstImageAt;
      },
      get mapReadyAt() {
        return svc.mapReadyAt;
      },
      get queued() {
        return svc.queue.length;
      },
      /** this tab records the session (holds its lock); false while another tab does */
      get writer() {
        return svc.writer;
      },
      recordHere: () => svc.recordHere(),
      flush: () => svc.flush(),
      newReading: () => svc.newReading(),
      state: () => svc.state,
      remaining: () => svc.remaining(),
      jump: (key: string) => svc.jumpToOrgan(key),
      nextGap: () => svc.nextGap(),
      setRadiological: (on: boolean) => svc.setRadiological(on),
      service: svc,
    } as Hooks;
  }
}
