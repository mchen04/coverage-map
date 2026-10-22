import { PubSubService } from '@ohif/core';
import { utilities as csUtils } from '@cornerstonejs/core';

import { CovmapApi } from './api';
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
  CovEvent,
  Coverage,
  CoverageState,
  Organ,
  ReaderAction,
  ReaderLayer,
  Rules,
  Sample,
  Volume,
  WindowClass,
} from './types';

const POST_INTERVAL_MS = 400;
const MAX_QUEUE = 20000;
const CLASS_ORDER: WindowClass[] = ['soft', 'lung', 'bone'];
const NO_READER: ReaderLayer = { reader: null, flagged: false, note: null };

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
  private t0 = 0;
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
    this.t0 = performance.now();
    this.lookupTried.clear();
    this.pending.clear();
    this.localReader = {};
    this.localFinish = null;
    this.firstImageAt = null;
    this.mapReadyAt = null;
    document.addEventListener('visibilitychange', this.onVisibility);
    window.addEventListener('pagehide', this.onPageHide);
    this.timer = setInterval(() => this.tick(), POST_INTERVAL_MS);
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
    this.stopReplay();
    document.removeEventListener('visibilitychange', this.onVisibility);
    window.removeEventListener('pagehide', this.onPageHide);
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
    return Math.round((performance.now() - this.t0) * 10) / 10;
  }

  private enqueue(kind: CovEvent['kind'], fields: Record<string, unknown>): CovEvent {
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
    if (!this.state.caseId && s.series) {
      this.lookup(s.series, s.study);
    }
    const { study, ...fields } = s;
    this.enqueue('view', fields);
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
    this.enqueue('end', {});
    const sessionId = this.state.sessionId;
    const events = this.queue.splice(0).map(e => this.stamp(e));
    if (!this.api.beacon(sessionId, events, this.nowMs())) {
      this.api.postEvents(sessionId, events, this.nowMs(), true).catch(() => {});
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

  /** POST queued events (or a heartbeat) and take the returned coverage. */
  public async flush(keepalive = false): Promise<void> {
    while (this.inflight) {
      await this.inflight;
    }
    const sessionId = this.state.sessionId;
    if (!sessionId) {
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

  private async openCase(caseId: string): Promise<void> {
    const [organs, volume, samples] = await Promise.all([
      this.api.organs(caseId),
      this.api.volume(caseId),
      this.api.samples(caseId).catch(() => [] as Sample[]),
    ]);
    this.volume = volume;
    this.sopToK = new Map(volume.sop_uids.map((s, k) => [s, k]));
    Object.assign(this.state, {
      caseId,
      seriesUid: volume.series_uid,
      studyUid: volume.study_uid,
      organs: organs.organs ?? [],
      samples: Array.isArray(samples) ? samples : [],
      error: null,
    });
    const { session } = await this.api.createSession(caseId, 'live reading (OHIF)');
    Object.assign(this.state, {
      sessionId: session,
      sessionLabel: 'Live reading',
      sessionSource: 'ohif',
      phase: 'ready',
    });
    this.state.reader = this.mergeReader(null);
    this.emit();
    this.flush();
  }

  private async restartSession(label: string, source: string, fromSample?: Sample): Promise<void> {
    const caseId = this.state.caseId;
    if (!caseId) {
      return;
    }
    this.stopReplay();
    this.switching = true;
    try {
      await this.openSession(caseId, label, source, fromSample);
    } finally {
      this.switching = false;
    }
  }

  private async openSession(
    caseId: string,
    label: string,
    source: string,
    fromSample?: Sample
  ): Promise<void> {
    if (this.state.sessionId) {
      this.enqueue('end', {});
      await this.flush();
    }
    this.queue = [];
    this.pending.clear();
    this.localReader = {};
    this.localFinish = null;
    const { session } = await this.api.createSession(
      caseId,
      fromSample ? `sample ${fromSample.name} + live` : 'live reading (OHIF)',
      fromSample?.name
    );
    // Live events restart at t=0/seq=0; for sample sessions the server shifts them after
    // the prefilled sample events.
    this.seq = 0;
    this.t0 = performance.now();
    Object.assign(this.state, {
      sessionId: session,
      sessionLabel: label,
      sessionSource: source,
      coverage: null,
      finishOpen: false,
      eventsSent: 0,
    });
    this.state.reader = this.mergeReader(null);
    this.emit();
    this.tracker.recaptureAll();
    await this.flush();
  }

  public newReading(): Promise<void> {
    return this.restartSession('Live reading', 'ohif');
  }

  public loadSample(name: string): Promise<void> {
    const sample = this.state.samples.find(s => s.name === name);
    if (!sample) {
      return Promise.resolve();
    }
    return this.restartSession(`Sample: ${sample.label}`, sample.source, sample);
  }

  public exportUrl(format: 'jsonl' | 'json' | 'csv'): string | null {
    return this.state.sessionId ? this.api.exportUrl(this.state.sessionId, format) : null;
  }

  public async applyRules(rules: Partial<Rules>): Promise<void> {
    if (!this.state.sessionId) {
      return;
    }
    await this.flush();
    const cov = await this.api.rules(this.state.sessionId, rules);
    this.takeCoverage(cov);
  }

  // ---------------------------------------------------------------- replay

  public async startReplay(frames = 40): Promise<void> {
    if (!this.state.sessionId) {
      return;
    }
    await this.flush();
    const { frames: list } = await this.api.timeline(this.state.sessionId, frames);
    if (!list?.length) {
      this.notify('Nothing to replay yet.');
      return;
    }
    this.state.replay = { frames: list, index: 0, playing: false };
    this.emit();
    this.playReplay();
  }

  public setReplayIndex(index: number): void {
    const r = this.state.replay;
    if (!r) {
      return;
    }
    r.index = Math.max(0, Math.min(r.frames.length - 1, index));
    this.state.replay = { ...r };
    this.emit();
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

  public stopReplay(): void {
    if (this.replayTimer) {
      clearInterval(this.replayTimer);
      this.replayTimer = null;
    }
    if (this.state.replay) {
      this.state.replay = null;
      this.emit();
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
    const ev = this.enqueue('annotation', fields);
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
      const ids = vp.getImageIds?.() ?? [];
      return !!series && ids.length > 0 && seriesOf(ids[0]).series === series;
    });
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

  public async jumpToOrgan(key: string): Promise<void> {
    const organ = this.state.organs.find(o => o.key === key);
    if (!organ) {
      return;
    }
    this.state.selected = key;
    this.emit();
    const preset = CLASS_PRESET[organ.windows?.[0]] ?? PRESETS.soft;
    let targets = this.caseViewports();
    if (!targets.length) {
      targets = await this.showCaseSeries();
    }
    const { lower, upper } = csUtils.windowLevel.toLowHighRange(preset.ww, preset.wc);
    for (const vp of targets) {
      try {
        vp.setProperties({ voiRange: { lower, upper } });
        if (isStack(vp)) {
          const ids: string[] = vp.getImageIds();
          let idx = ids.findIndex(id => sopOf(id) === organ.center_sop);
          if (idx < 0 && this.volume) {
            const sop = this.volume.sop_uids[organ.center_k];
            idx = ids.findIndex(id => sopOf(id) === sop);
          }
          if (idx >= 0 && idx !== vp.getCurrentImageIdIndex()) {
            await vp.setImageIdIndex(idx);
          } else {
            vp.render();
          }
        } else {
          vp.jumpToWorld(organ.centroid_lps);
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
      state: () => svc.state,
      remaining: () => svc.remaining(),
      jump: (key: string) => svc.jumpToOrgan(key),
      nextGap: () => svc.nextGap(),
      service: svc,
    } as Hooks;
  }
}
