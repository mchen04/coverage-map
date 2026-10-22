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
import vtkDataArray from '@kitware/vtk.js/Common/Core/DataArray';
import vtkPolyData from '@kitware/vtk.js/Common/DataModel/PolyData';
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

/** Canonical views: from the front (anterior), back, patient right side, patient left side. */
export type MapView = 'front' | 'back' | 'right' | 'left';

/** Patient direction at each screen edge (R L A P S I), null when the view is oblique. */
export interface Orientation {
  left: string | null;
  right: string | null;
  top: string | null;
  bottom: string | null;
}

export interface OrganMap {
  loadMeshes(src: string | { meta: MeshesJson; buffer: ArrayBuffer }): Promise<LoadResult>;
  setOrganInfo(organs: OrganInfo[]): void;
  /** key -> status, or key -> {status, reader, flagged} */
  setStatuses(map: Record<string, Status | OrganVisual>): void;
  setHighlight(key: string | null): void;
  /** Hide these organs (not drawn, not pickable); the others are shown. */
  setHidden(keys: Iterable<string>): void;
  onPick(cb: (e: PickEvent) => void): () => void;
  /** Called after every camera change (rotate, pan, zoom, view buttons). */
  onCamera(cb: (o: Orientation) => void): () => void;
  orientation(): Orientation;
  /** Fit the whole model in a canonical view (default: front). */
  resetCamera(view?: MapView): void;
  resize(): void;
  render(): void;
  keys(): string[];
  drawnCount(): number;
  visibleCount(): number;
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
/** Not applicable: a faint violet wireframe, so it never reads as skipped gray. */
export const NA_COLOR = '#b48ef0';

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
    return 0.35;
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

/**
 * Smooth point normals of a triangle mesh (local point indices), with the same arithmetic in
 * the same order as vtk.js's vtkPolyDataNormals (so the same bits, the same pixels) but
 * without its per-triangle array allocations, which made it the largest single cost of
 * building the map.
 */
function pointNormals(points: Float32Array, tris: Uint32Array, base: number, nTris: number): Float32Array {
  const out = new Float32Array(points.length);
  for (let t = 0; t < nTris; t++) {
    const i1 = 3 * tris[base + t * 3];
    const i2 = 3 * tris[base + t * 3 + 1];
    const i3 = 3 * tris[base + t * 3 + 2];
    // vtkTriangle.computeNormal(v1, v2, v3)
    const ax = points[i3] - points[i2];
    const ay = points[i3 + 1] - points[i2 + 1];
    const az = points[i3 + 2] - points[i2 + 2];
    const bx = points[i1] - points[i2];
    const by = points[i1 + 1] - points[i2 + 1];
    const bz = points[i1 + 2] - points[i2 + 2];
    let nx = ay * bz - az * by;
    let ny = az * bx - ax * bz;
    let nz = ax * by - ay * bx;
    const len = Math.sqrt(nx * nx + ny * ny + nz * nz);
    if (len !== 0) {
      nx /= len;
      ny /= len;
      nz /= len;
    }
    out[i1] += nx;
    out[i1 + 1] += ny;
    out[i1 + 2] += nz;
    out[i2] += nx;
    out[i2 + 1] += ny;
    out[i2 + 2] += nz;
    out[i3] += nx;
    out[i3 + 1] += ny;
    out[i3 + 2] += nz;
  }
  for (let i = 0; i < out.length; i += 3) {
    // vtkMath.normalize
    const x = out[i];
    const y = out[i + 1];
    const z = out[i + 2];
    const den = Math.sqrt(x * x + y * y + z * z);
    if (den !== 0) {
      out[i] = x / den;
      out[i + 1] = y / den;
      out[i + 2] = z / den;
    }
  }
  return out;
}

interface Entry {
  key: string;
  actor: any;
  visual: OrganVisual;
  opacity: number;
  hidden: boolean;
}

// LPS: +x patient left, +y posterior, +z superior
const AXIS_LABELS: Array<[string, string]> = [
  ['L', 'R'],
  ['P', 'A'],
  ['S', 'I'],
];

function axisLabel(v: number[]): string | null {
  let i = 0;
  for (let j = 1; j < 3; j++) {
    if (Math.abs(v[j]) > Math.abs(v[i])) {
      i = j;
    }
  }
  const len = Math.hypot(v[0], v[1], v[2]) || 1;
  // only label an edge when the view is within ~35 degrees of a patient axis
  if (Math.abs(v[i]) / len < 0.82) {
    return null;
  }
  return v[i] > 0 ? AXIS_LABELS[i][0] : AXIS_LABELS[i][1];
}

/** Camera offset direction (from the focal point) and view-up for each canonical view. */
const VIEWS: Record<MapView, { dir: [number, number, number]; widthAxis: 0 | 1 }> = {
  front: { dir: [0, -1, 0], widthAxis: 0 },
  back: { dir: [0, 1, 0], widthAxis: 0 },
  right: { dir: [-1, 0, 0], widthAxis: 1 },
  left: { dir: [1, 0, 0], widthAxis: 1 },
};

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
    // Rotate about the focal point (the model centre after a reset, the view centre after a
    // pan) so the model stays in view; the default centre is the world origin, far from it.
    style.addMouseManipulator(
      vtkMouseCameraTrackballRotateManipulator.newInstance({
        button: 1,
        useFocalPointAsCenterOfRotation: true,
      })
    );
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
  const cameraCbs = new Set<(o: Orientation) => void>();
  let highlight: string | null = null;
  let hiddenKeys = new Set<string>();
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
    const na = e.visual.reader === 'na';
    e.opacity = hot ? 1 : baseOpacity(info.get(e.key), e.visual);
    prop.setOpacity(e.opacity);
    // N/A: wireframe (hatched look), clearly unlike a solid skipped organ
    prop.setRepresentation(na && !hot ? 1 : 2);
    prop.setLineWidth(1);
    e.actor.setVisibility(!e.hidden);
    e.actor.setPickable(!e.hidden);
    prop.setAmbient(hot ? 0.45 : 0.12);
    prop.setDiffuse(0.85);
    prop.setSpecular(0.15);
    prop.setSpecularPower(20);
  };

  let renderQueued = false;
  const render = () => {
    renderQueued = false;
    if (!disposed) {
      renderWindow.render();
    }
  };
  // Updates (load, camera, statuses, highlight, resize) request a render; one render per
  // frame covers all of them. render() still draws immediately.
  const requestRender = () => {
    if (!renderQueued) {
      renderQueued = true;
      requestAnimationFrame(() => renderQueued && render());
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
    requestRender();
  };

  const resetCamera = (view: MapView = 'front') => {
    if (!bounds) {
      return;
    }
    const [x0, x1, y0, y1, z0, z1] = bounds;
    const c = [(x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2];
    const { dir, widthAxis } = VIEWS[view] ?? VIEWS.front;
    const cam = renderer.getActiveCamera();
    cam.setParallelProjection(true);
    cam.setFocalPoint(c[0], c[1], c[2]);
    // LPS: anterior is -y; viewing from the front puts patient right on screen left.
    cam.setPosition(c[0] + dir[0] * 1000, c[1] + dir[1] * 1000, c[2] + dir[2] * 1000);
    cam.setViewUp(0, 0, 1);
    renderer.resetCamera(bounds);
    const rect = container.getBoundingClientRect();
    const aspect = rect.width > 0 && rect.height > 0 ? rect.width / rect.height : 1;
    const halfH = (z1 - z0) / 2;
    const halfW = (widthAxis === 0 ? x1 - x0 : y1 - y0) / 2;
    cam.setParallelScale(Math.max(halfH, halfW / aspect) * 1.06);
    renderer.resetCameraClippingRange();
    requestRender();
  };

  const orientation = (): Orientation => {
    const cam = renderer.getActiveCamera();
    const d = cam.getDirectionOfProjection();
    const u = cam.getViewUp();
    const right = [d[1] * u[2] - d[2] * u[1], d[2] * u[0] - d[0] * u[2], d[0] * u[1] - d[1] * u[0]];
    const neg = (v: number[]) => v.map(x => -x);
    return {
      left: axisLabel(neg(right)),
      right: axisLabel(right),
      top: axisLabel(u),
      bottom: axisLabel(neg(u)),
    };
  };
  let cameraQueued = false;
  const cameraSub = renderer.getActiveCamera().onModified(() => {
    if (cameraQueued || !cameraCbs.size) {
      return;
    }
    cameraQueued = true;
    requestAnimationFrame(() => {
      cameraQueued = false;
      if (!disposed) {
        const o = orientation();
        cameraCbs.forEach(cb => cb(o));
      }
    });
  });

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
      poly.getPointData().setNormals(
        vtkDataArray.newInstance({
          numberOfComponents: 3,
          name: 'Normals',
          values: pointNormals(points, tris, base, o.n_tris),
        })
      );
      const mapper = vtkMapper.newInstance({ scalarVisibility: false });
      mapper.setInputData(poly);
      const actor = vtkActor.newInstance();
      actor.setMapper(mapper);
      const entry: Entry = {
        key: o.key,
        actor,
        visual: { status: 'skipped' },
        opacity: 1,
        hidden: hiddenKeys.has(o.key),
      };
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
    requestRender();
  };

  const setStatuses = (map: Record<string, Status | OrganVisual>) => {
    entries.forEach(e => {
      const v = map[e.key];
      e.visual = typeof v === 'string' ? { status: v } : { status: 'skipped', ...(v ?? {}) };
      applyVisual(e);
    });
    requestRender();
  };

  const setHidden = (keys: Iterable<string>) => {
    hiddenKeys = new Set(keys);
    entries.forEach(e => {
      const h = hiddenKeys.has(e.key);
      if (h !== e.hidden) {
        e.hidden = h;
        applyVisual(e);
      }
    });
    requestRender();
  };

  const setHighlight = (key: string | null) => {
    if (key === highlight) {
      return;
    }
    const prev = highlight;
    highlight = key;
    [prev, key].forEach(k => k && entries.get(k) && applyVisual(entries.get(k)));
    requestRender();
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
    const translucent = [...entries.values()].filter(e => !e.hidden && e.opacity < 0.99);
    translucent.forEach(e => e.actor.setPickable(false));
    let key: string | null = null;
    try {
      key = await selectOnce();
    } finally {
      translucent.forEach(e => e.actor.setPickable(!e.hidden));
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
    cameraSub?.unsubscribe?.();
    cameraCbs.clear();
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
    setHidden,
    onPick: cb => {
      pickCbs.add(cb);
      return () => pickCbs.delete(cb);
    },
    onCamera: cb => {
      cameraCbs.add(cb);
      return () => cameraCbs.delete(cb);
    },
    orientation,
    resetCamera,
    resize,
    render,
    keys: () => [...entries.keys()],
    drawnCount: () => entries.size,
    visibleCount: () => [...entries.values()].filter(e => !e.hidden).length,
    dispose,
  };
}
