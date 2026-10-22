import { PubSubService } from '@ohif/core';
import { utilities as csUtils } from '@cornerstonejs/core';

import { CovmapApi } from './api';
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
import { OutlineOverlay } from './outline';
import { CLASS_PRESET, PRESETS, type Preset } from './presets';
import type {
  CaseInfo,
  CovEvent,
  Coverage,
  CoverageState,
  Gap,
  Organ,
  ReaderAction,
  ReaderLayer,
  Rules,
  Sample,
  SessionInfo,
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
const LIVE_LABEL = 'Live reading';

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
  };
}

function applyAction(layer: ReaderLayer, action: ReaderAction, note?: string | null): ReaderLayer {
  switch (action) {
    case 'done':
      return { ...layer, reader: 'done' };
    case 'na':
      return { ...layer, reader: 'na' };
    case 'flag':
      return { ...layer, flagged: true, note: note ?? layer.note ?? null };
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
  /** stack viewports already checked for radiological orientation (viewport -> stack key) */
  private oriented = new Map<string, string>();
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
  private localFinish: { confirmed: boolean } | null = null;
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
    this.oriented.clear();
    this.order = null;
    this.lookupTried.clear();
    this.pending.clear();
    this.localReader = {};
    this.localFinish = null;
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
    if (this.state.sessionId) {
      this.enqueue('end', {});
      this.finalFlush();
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
   *  switching; reader events always are. */
  private enqueue(kind: CovEvent['kind'], fields: Record<string, unknown>): CovEvent | null {
    if (!this.recording && (kind === 'view' || kind === 'hidden')) {
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
    if (s.vp_type === 'stack' && this.orient(s)) {
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
    let ids: string[] = [];
    try {
      ids = vp.getImageIds?.() ?? [];
    } catch {
      return false;
    }
    const key = `${ids.length}|${ids[0] ?? ''}`;
    if (this.oriented.get(s.viewport) === key) {
      return false;
    }
    this.oriented.set(s.viewport, key);
    if (!this.radiological || Math.abs(s.normal[2]) < 0.9) {
      return false;
    }
    const [tl, tr, , bl] = s.corners_lps;
    const posteriorUp = tl[1] - bl[1] > 0; // screen up points to +y (posterior)
    const rightIsPatientRight = tr[0] - tl[0] < 0; // screen right points to -x (patient right)
    if (!posteriorUp && !rightIsPatientRight) {
      return false;
    }
    try {
      const before = vp.getCamera();
      if (posteriorUp) {
        vp.flip({ flipVertical: true });
      }
      if (rightIsPatientRight) {
        vp.flip({ flipHorizontal: true });
      }
      const after = vp.getCamera();
      if (before.flipVertical === after.flipVertical && before.flipHorizontal === after.flipHorizontal) {
        this.oriented.delete(s.viewport); // image data not ready: try again on the next render
        return false;
      }
      return true;
    } catch (e) {
      console.warn('covmap: could not set radiological orientation', e);
      return false;
    }
  }

  /** Radiological display on (default) or the acquisition orientation (research/test hook). */
  public setRadiological(on: boolean): void {
    this.radiological = on;
    this.oriented.clear();
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
    }
  };

  private onPageHide = () => {
    if (!this.state.sessionId) {
      return;
    }
    // 'end' is the session's last event: nothing displayed after it is recorded (the
    // visibilitychange that follows would otherwise post a 'hidden' racing the beacon)
    this.setRecording(false);
    this.enqueue('end', {});
    const sessionId = this.state.sessionId;
    const events = this.queue.splice(0).map(e => this.stamp(e));
    if (!this.api.beacon(sessionId, events, this.nowMs())) {
      this.api.postEvents(sessionId, events, this.nowMs(), true).catch(() => {});
    }
  };

  /** Back from the back/forward cache: the page is shown again, record again. */
  private onPageShow = (e: PageTransitionEvent) => {
    if (e.persisted && this.state.sessionId && !this.state.replay && !this.switching) {
      this.resumeRecording();
    }
  };

  /** Send what is queued now (mode exit), after any request in flight, with keepalive. */
  private finalFlush(): void {
    const sessionId = this.state.sessionId;
    if (!sessionId) {
      return;
    }
    const events = this.queue.splice(0).map(e => this.stamp(e));
    const now = this.nowMs();
    const send = () =>
      this.api.postEvents(sessionId, events, now, true).catch(() => {
        this.api.beacon(sessionId, events, now);
      });
    (this.inflight ?? Promise.resolve()).then(send, send);
  }

  private stamp(e: CovEvent): CovEvent {
    return { ...e, session: this.state.sessionId, case: this.state.caseId };
  }

  private tick(): void {
    if (this.state.phase === 'ready' && this.state.sessionId && !this.inflight && !this.switching) {
      this.flush();
    }
  }

  /** POST queued events (or a heartbeat) and take the returned coverage. Nothing is sent
   *  while a session switch is in progress unless `force` (the old session's end). */
  public async flush(keepalive = false, force = false): Promise<void> {
    while (this.inflight) {
      await this.inflight;
    }
    const sessionId = this.state.sessionId;
    if (!sessionId || (this.switching && !force)) {
      return;
    }
    const events = this.queue.splice(0).map(e => this.stamp(e));
    const maxSeq = events.length ? events[events.length - 1].seq : -1;
    const now = this.nowMs();
    const t = performance.now();
    this.inflight = (async () => {
      try {
        const cov = await this.api.postEvents(sessionId, events, now, keepalive);
        if (sessionId !== this.state.sessionId) {
          return;
        }
        this.state.eventsSent += events.length;
        this.state.lastPostMs = Math.round(performance.now() - t);
        this.pending.forEach((p, key) => p.seq <= maxSeq && this.pending.delete(key));
        this.takeCoverage(cov);
      } catch (e) {
        const status = (e as { status?: number }).status;
        if (status === 400) {
          console.error('covmap: server rejected an event batch; dropped', e, events);
        } else {
          if (sessionId === this.state.sessionId) {
            this.queue.unshift(...events);
          }
          console.warn('covmap: event POST failed, will retry', e);
        }
      }
    })().finally(() => {
      this.inflight = null;
    });
    return this.inflight;
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

  /** The case's last session if the server still has it, else a new live session. */
  private async resumeOrCreate(
    caseId: string
  ): Promise<{ session: string; info: SessionInfo | null; rec: StoredSession | null }> {
    const rec = this.recall(caseId);
    if (rec) {
      try {
        const info = await this.api.session(rec.session);
        if (info?.case === caseId) {
          return { session: rec.session, info, rec };
        }
      } catch (e) {
        console.info('covmap: last session not resumable, starting a new one', e);
      }
    }
    const { session } = await this.api.createSession(caseId, 'live reading (OHIF)');
    return { session, info: null, rec: null };
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
    const { session, info, rec } = await sessionReq;
    if (info) {
      // Continue the stored session after its last event: events shown before it was found
      // (a few frames of this page load) are dropped and the current state re-captured.
      this.queue = [];
      this.seq = (info.last_seq ?? -1) + 1;
      this.t0 = performance.now() - ((info.last_t_ms ?? 0) + 1);
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
    if (info) {
      this.tracker.recaptureAll();
    }
    this.flush();
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
      if (this.state.sessionId) {
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
      return;
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
    let list: TimelineFrame[] = [];
    try {
      await this.flush();
      list = (await this.api.timeline(sessionId, frames)).frames ?? [];
    } catch (e) {
      console.warn('covmap: timeline failed', e);
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
    this.state.replay = { frames: list, index: 0, playing: false };
    this.emit();
    this.showReplayFrame();
    this.playReplay();
  }

  private resumeRecording(): void {
    this.setRecording(true);
    this.tracker.recaptureAll();
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

  public finished(): { confirmed: boolean } | null {
    return this.localFinish ?? this.state.coverage?.summary?.finished ?? null;
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
    const fields: Record<string, unknown> = { organ: key, action };
    if (note) {
      fields.note = note;
    }
    const ev = this.enqueue('annotation', fields)!;
    const prev = this.state.reader[key] ?? NO_READER;
    const layer = applyAction(prev, action, note);
    this.localReader[key] = applyAction(this.localReader[key] ?? NO_READER, action, note);
    this.pending.set(key, { layer, seq: ev.seq });
    this.state.reader = { ...this.state.reader, [key]: layer };
    this.emit();
  }

  private setLayer(key: string, next: ReaderLayer): void {
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
      this.annotate(key, 'flag', next.note);
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
    this.state.finishOpen = open;
    this.emit();
  }

  public async finish(confirmed: boolean): Promise<void> {
    this.enqueue('finish', { confirmed, remaining: this.remaining() });
    this.state.finishOpen = false;
    if (confirmed) {
      this.localFinish = { confirmed: true };
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
        const gap = await this.api.gap(sessionId, key, this.nowMs(), ctl.signal);
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
   * Select an organ and show it: the slice where it still has the most unseen voxels (from
   * the server, after sending what is queued) in its window; the organ centre when nothing is
   * unseen or the gap lookup fails.
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
    let targets = this.caseViewports();
    if (!targets.length) {
      targets = await this.showCaseSeries();
    }
    const range = csUtils.windowLevel.toLowHighRange(preset.ww, preset.wc);
    for (const vp of targets) {
      try {
        await this.showSlice(vp, sop, k, range, organ);
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
