/**
 * vtk.js organ coverage map: surface meshes from `meshes.json` + `meshes.bin`
 * (docs/SPEC.md "Meshes"), colored by measured status. Framework-free so the
 * OHIF panel and the headless render page share exactly this code.
 * Research use only.
 */
import '@kitware/vtk.js/Rendering/OpenGL/Profiles/Geometry';
import vtkActor from '@kitware/vtk.js/Rendering/Core/Actor';
import vtkMapper from '@kitware/vtk.js/Rendering/Core/Mapper';
import vtkRenderer from '@kitware/vtk.js/Rendering/Core/Renderer';
import vtkRenderWindow from '@kitware/vtk.js/Rendering/Core/RenderWindow';
import vtkRenderWindowInteractor from '@kitware/vtk.js/Rendering/Core/RenderWindowInteractor';
import vtkOpenGLRenderWindow from '@kitware/vtk.js/Rendering/OpenGL/RenderWindow';
import vtkInteractorStyleManipulator from '@kitware/vtk.js/Interaction/Style/InteractorStyleManipulator';
import vtkMouseCameraTrackballRotateManipulator from '@kitware/vtk.js/Interaction/Manipulators/MouseCameraTrackballRotateManipulator';
import vtkMouseCameraTrackballZoomManipulator from '@kitware/vtk.js/Interaction/Manipulators/MouseCameraTrackballZoomManipulator';
import vtkMouseCameraTrackballPanManipulator from '@kitware/vtk.js/Interaction/Manipulators/MouseCameraTrackballPanManipulator';
import vtkPolyData from '@kitware/vtk.js/Common/DataModel/PolyData';
import vtkPolyDataNormals from '@kitware/vtk.js/Filters/Core/PolyDataNormals';
import { FieldAssociations } from '@kitware/vtk.js/Common/DataModel/DataSet/Constants';

export type Status = 'reviewed' | 'partial' | 'skipped';
export type ReaderState = 'done' | 'na' | null;

export interface OrganVisual {
  status?: Status;
  reader?: ReaderState;
  flagged?: boolean;
}

export interface MeshOrgan {
  id: number;
  key: string;
  points_offset: number;
  n_points: number;
  tris_offset: number;
  n_tris: number;
}

export interface MeshesJson {
  organs: MeshOrgan[];
  bounds_lps: number[];
}

export interface OrganInfo {
  key: string;
  name?: string;
  group?: string;
  windows?: string[];
}

export interface PickEvent {
  key: string | null;
  kind: 'hover' | 'click';
  clientX: number;
  clientY: number;
}

export interface LoadResult {
  organs: number;
  triangles: number;
  points: number;
  fetch_ms: number;
  build_ms: number;
}

export interface OrganMapOptions {
  /** rgb 0..1 */
  background?: [number, number, number];
  interactive?: boolean;
}

export interface OrganMap {
  loadMeshes(src: string | { meta: MeshesJson; buffer: ArrayBuffer }): Promise<LoadResult>;
  setOrganInfo(organs: OrganInfo[]): void;
  /** key -> status, or key -> {status, reader, flagged} */
  setStatuses(map: Record<string, Status | OrganVisual>): void;
  setHighlight(key: string | null): void;
  onPick(cb: (e: PickEvent) => void): () => void;
  resetCamera(): void;
  resize(): void;
  render(): void;
  keys(): string[];
  drawnCount(): number;
  dispose(): void;
}

/** Accessible on OHIF's near-black theme: lightness differs as well as hue. */
export const STATUS_COLORS: Record<Status, string> = {
  reviewed: '#34c77b',
  partial: '#f2c94c',
  skipped: '#8a93a3',
};
/** Reader attestation ("done") — deliberately a different hue from measured green. */
export const ATTESTED_COLOR = '#4cc9f0';
export const NA_COLOR = '#4a5160';

export function hexToRgb(hex: string): [number, number, number] {
  const v = parseInt(hex.replace('#', ''), 16);
  return [((v >> 16) & 255) / 255, ((v >> 8) & 255) / 255, (v & 255) / 255];
}

function visualColor(v: OrganVisual): string {
  if (v.reader === 'na') {
    return NA_COLOR;
  }
  if (v.reader === 'done' && v.status !== 'reviewed') {
    return ATTESTED_COLOR;
  }
  return STATUS_COLORS[v.status ?? 'skipped'];
}

function baseOpacity(info: OrganInfo | undefined, v: OrganVisual): number {
  if (v.reader === 'na') {
    return 0.12;
  }
  const w = info?.windows ?? [];
  if (w.length === 1 && w[0] === 'bone') {
    return 0.4;
  }
  if (w.includes('lung') && !w.includes('soft')) {
    return 0.45;
  }
  return 1;
}

export async function fetchMeshes(base: string): Promise<{ meta: MeshesJson; buffer: ArrayBuffer }> {
  const root = base.replace(/\/$/, '');
  const [metaRes, binRes] = await Promise.all([
    fetch(`${root}/meshes.json`),
    fetch(`${root}/meshes.bin`),
  ]);
  if (!metaRes.ok || !binRes.ok) {
    throw new Error(`mesh fetch failed: ${metaRes.status}/${binRes.status} at ${root}`);
  }
  return { meta: await metaRes.json(), buffer: await binRes.arrayBuffer() };
}

interface Entry {
  key: string;
  actor: any;
  visual: OrganVisual;
  opacity: number;
}

export function createOrganMap(container: HTMLElement, opts: OrganMapOptions = {}): OrganMap {
  const interactive = opts.interactive ?? true;
  const renderWindow = vtkRenderWindow.newInstance();
  const renderer = vtkRenderer.newInstance();
  const [br, bg, bb] = opts.background ?? [0.03, 0.04, 0.08];
  renderer.setBackground(br, bg, bb, 1);
  renderWindow.addRenderer(renderer);
  const glWindow = vtkOpenGLRenderWindow.newInstance();
  renderWindow.addView(glWindow);
  glWindow.setContainer(container);
  const canvas = glWindow.getCanvas() as HTMLCanvasElement;
  canvas.style.display = 'block';
  canvas.style.height = '100%';

  let interactor: any = null;
  if (interactive) {
    interactor = vtkRenderWindowInteractor.newInstance();
    const style = vtkInteractorStyleManipulator.newInstance();
    style.addMouseManipulator(vtkMouseCameraTrackballRotateManipulator.newInstance({ button: 1 }));
    style.addMouseManipulator(
      vtkMouseCameraTrackballPanManipulator.newInstance({ button: 1, shift: true })
    );
    style.addMouseManipulator(vtkMouseCameraTrackballPanManipulator.newInstance({ button: 2 }));
    style.addMouseManipulator(
      vtkMouseCameraTrackballZoomManipulator.newInstance({ button: 3, scrollEnabled: true })
    );
    interactor.setInteractorStyle(style);
    interactor.setView(glWindow);
    interactor.initialize();
    interactor.bindEvents(container);
  }

  const entries = new Map<string, Entry>();
  const actorToKey = new Map<any, string>();
  const info = new Map<string, OrganInfo>();
  const pickCbs = new Set<(e: PickEvent) => void>();
  let highlight: string | null = null;
  let bounds: number[] | null = null;
  let disposed = false;

  const selector = glWindow.createSelector();
  selector.setFieldAssociation(FieldAssociations.FIELD_ASSOCIATION_CELLS);
  selector.setCaptureZValues(false);

  const applyVisual = (e: Entry) => {
    const prop = e.actor.getProperty();
    const [r, g, b] = hexToRgb(visualColor(e.visual));
    prop.setColor(r, g, b);
    const hot = highlight === e.key;
    e.opacity = hot ? 1 : baseOpacity(info.get(e.key), e.visual);
    prop.setOpacity(e.opacity);
    prop.setAmbient(hot ? 0.45 : 0.12);
    prop.setDiffuse(0.85);
    prop.setSpecular(0.15);
    prop.setSpecularPower(20);
  };

  const render = () => {
    if (!disposed) {
      renderWindow.render();
    }
  };

  const resize = () => {
    if (disposed) {
      return;
    }
    const rect = container.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.floor(rect.width * dpr));
    const h = Math.max(1, Math.floor(rect.height * dpr));
    const [cw, ch] = glWindow.getSize();
    if (cw !== w || ch !== h) {
      glWindow.setSize(w, h);
    }
    render();
  };

  const resetCamera = () => {
    if (!bounds) {
      return;
    }
    const [x0, x1, y0, y1, z0, z1] = bounds;
    const c = [(x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2];
    const cam = renderer.getActiveCamera();
    cam.setParallelProjection(true);
    cam.setFocalPoint(c[0], c[1], c[2]);
    // LPS: anterior is -y; viewing from the front puts patient right on screen left.
    cam.setPosition(c[0], c[1] - 1000, c[2]);
    cam.setViewUp(0, 0, 1);
    renderer.resetCamera(bounds);
    const rect = container.getBoundingClientRect();
    const aspect = rect.width > 0 && rect.height > 0 ? rect.width / rect.height : 1;
    const halfH = (z1 - z0) / 2;
    const halfW = (x1 - x0) / 2;
    cam.setParallelScale(Math.max(halfH, halfW / aspect) * 1.06);
    renderer.resetCameraClippingRange();
    render();
  };

  const loadMeshes = async (src: string | { meta: MeshesJson; buffer: ArrayBuffer }) => {
    const t0 = performance.now();
    const { meta, buffer } = typeof src === 'string' ? await fetchMeshes(src) : src;
    const t1 = performance.now();
    entries.forEach(e => renderer.removeActor(e.actor));
    entries.clear();
    actorToKey.clear();

    const nPts = meta.organs.reduce((s, o) => s + o.n_points, 0);
    const nTris = meta.organs.reduce((s, o) => s + o.n_tris, 0);
    if (buffer.byteLength < nPts * 12 + nTris * 12) {
      throw new Error(`meshes.bin too short: ${buffer.byteLength} bytes`);
    }
    const pts = new Float32Array(buffer, 0, nPts * 3);
    const tris = new Uint32Array(buffer, nPts * 12, nTris * 3);

    for (const o of meta.organs) {
      if (!o.n_points || !o.n_tris) {
        continue;
      }
      const points = pts.slice(o.points_offset * 3, (o.points_offset + o.n_points) * 3);
      const cells = new Uint32Array(o.n_tris * 4);
      const base = o.tris_offset * 3;
      for (let t = 0; t < o.n_tris; t++) {
        cells[t * 4] = 3;
        cells[t * 4 + 1] = tris[base + t * 3];
        cells[t * 4 + 2] = tris[base + t * 3 + 1];
        cells[t * 4 + 3] = tris[base + t * 3 + 2];
      }
      const poly = vtkPolyData.newInstance();
      poly.getPoints().setData(points, 3);
      poly.getPolys().setData(cells);
      const normals = vtkPolyDataNormals.newInstance({ computeCellNormals: false });
      normals.setInputData(poly);
      const mapper = vtkMapper.newInstance({ scalarVisibility: false });
      mapper.setInputData(normals.getOutputData());
      const actor = vtkActor.newInstance();
      actor.setMapper(mapper);
      const entry: Entry = { key: o.key, actor, visual: { status: 'skipped' }, opacity: 1 };
      applyVisual(entry);
      renderer.addActor(actor);
      entries.set(o.key, entry);
      actorToKey.set(actor, o.key);
    }
    bounds = meta.bounds_lps;
    resize();
    resetCamera();
    return {
      organs: entries.size,
      triangles: nTris,
      points: nPts,
      fetch_ms: t1 - t0,
      build_ms: performance.now() - t1,
    };
  };

  const setOrganInfo = (organs: OrganInfo[]) => {
    info.clear();
    organs.forEach(o => info.set(o.key, o));
    entries.forEach(applyVisual);
    render();
  };

  const setStatuses = (map: Record<string, Status | OrganVisual>) => {
    entries.forEach(e => {
      const v = map[e.key];
      e.visual = typeof v === 'string' ? { status: v } : { status: 'skipped', ...(v ?? {}) };
      applyVisual(e);
    });
    render();
  };

  const setHighlight = (key: string | null) => {
    if (key === highlight) {
      return;
    }
    const prev = highlight;
    highlight = key;
    [prev, key].forEach(k => k && entries.get(k) && applyVisual(entries.get(k)));
    render();
  };

  // --- picking (hardware selector; hover throttled to one pick in flight) ---
  let picking = false;
  let pendingHover: PointerEvent | null = null;
  let downAt: [number, number] | null = null;
  let buttonsDown = false;

  const pickAt = async (clientX: number, clientY: number): Promise<string | null> => {
    if (!entries.size) {
      return null;
    }
    const rect = container.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const x = Math.round((clientX - rect.left) * dpr);
    const y = Math.round((rect.height - (clientY - rect.top)) * dpr);
    const selectOnce = async () => {
      const sel = await selector.selectAsync(renderer, x, y, x, y);
      const prop = sel?.[0]?.getProperties?.().prop;
      return prop ? (actorToKey.get(prop) ?? null) : null;
    };
    // Translucent organs (bones, lungs, N/A) are see-through: prefer an opaque organ behind them.
    const translucent = [...entries.values()].filter(e => e.opacity < 0.99);
    translucent.forEach(e => e.actor.setPickable(false));
    let key: string | null = null;
    try {
      key = await selectOnce();
    } finally {
      translucent.forEach(e => e.actor.setPickable(true));
    }
    if (!key && translucent.length) {
      key = await selectOnce();
    }
    return key;
  };

  const emit = (e: PickEvent) => pickCbs.forEach(cb => cb(e));

  const runHover = async () => {
    if (picking || !pendingHover) {
      return;
    }
    const ev = pendingHover;
    pendingHover = null;
    picking = true;
    try {
      const key = await pickAt(ev.clientX, ev.clientY);
      emit({ key, kind: 'hover', clientX: ev.clientX, clientY: ev.clientY });
    } finally {
      picking = false;
      if (pendingHover) {
        requestAnimationFrame(runHover);
      }
    }
  };

  const onMove = (ev: PointerEvent) => {
    if (buttonsDown || !pickCbs.size) {
      return;
    }
    pendingHover = ev;
    requestAnimationFrame(runHover);
  };
  const onDown = (ev: PointerEvent) => {
    buttonsDown = true;
    downAt = [ev.clientX, ev.clientY];
  };
  const onUp = async (ev: PointerEvent) => {
    buttonsDown = false;
    const d = downAt;
    downAt = null;
    if (!d || ev.button !== 0 || Math.hypot(ev.clientX - d[0], ev.clientY - d[1]) > 4) {
      return;
    }
    const key = await pickAt(ev.clientX, ev.clientY);
    emit({ key, kind: 'click', clientX: ev.clientX, clientY: ev.clientY });
  };
  const onLeave = (ev: PointerEvent) => {
    pendingHover = null;
    emit({ key: null, kind: 'hover', clientX: ev.clientX, clientY: ev.clientY });
  };
  if (interactive) {
    container.addEventListener('pointermove', onMove);
    container.addEventListener('pointerdown', onDown);
    window.addEventListener('pointerup', onUp);
    container.addEventListener('pointerleave', onLeave);
  }

  const dispose = () => {
    if (disposed) {
      return;
    }
    disposed = true;
    container.removeEventListener('pointermove', onMove);
    container.removeEventListener('pointerdown', onDown);
    window.removeEventListener('pointerup', onUp);
    container.removeEventListener('pointerleave', onLeave);
    pickCbs.clear();
    if (interactor) {
      interactor.unbindEvents();
      interactor.delete();
    }
    entries.forEach(e => {
      renderer.removeActor(e.actor);
      e.actor.getMapper()?.delete();
      e.actor.delete();
    });
    entries.clear();
    glWindow.setContainer(null);
    glWindow.delete();
    renderer.delete();
    renderWindow.delete();
  };

  return {
    loadMeshes,
    setOrganInfo,
    setStatuses,
    setHighlight,
    onPick: cb => {
      pickCbs.add(cb);
      return () => pickCbs.delete(cb);
    },
    resetCamera,
    resize,
    render,
    keys: () => [...entries.keys()],
    drawnCount: () => entries.size,
    dispose,
  };
}
