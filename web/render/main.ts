/**
 * Standalone organ-map page used by `covmap render` (headless Chromium). Same vtk.js
 * module as the OHIF panel. Inputs come from window.__COVMAP_RENDER_INIT (set by the
 * driver before load) or the query string (?case=&w=&h=); the result is published on
 * window.__render. Research use only.
 */
import {
  createOrganMap,
  fetchMeshes,
  type OrganVisual,
  type Status,
} from '@covmap/organ-map';

interface CoverageLike {
  organs?: Array<{ key: string; status?: Status; reader?: 'done' | 'na' | null; flagged?: boolean }>;
}

interface Init {
  case?: string;
  statuses?: Record<string, Status | OrganVisual>;
  coverage?: CoverageLike;
  width?: number;
  height?: number;
  background?: [number, number, number];
}

interface RenderResult {
  ready: boolean;
  error: string | null;
  case: string | null;
  organsDrawn: number;
  organsListed: number;
  triangles: number;
  statusCounts: Record<string, number>;
  timings: Record<string, number>;
  renderer: string | null;
  devicePixelRatio: number;
}

declare global {
  interface Window {
    __COVMAP_RENDER_INIT?: Init;
    __render?: RenderResult;
  }
}

function webglRenderer(): string | null {
  const gl = document.createElement('canvas').getContext('webgl2');
  if (!gl) {
    return null;
  }
  const ext = gl.getExtension('WEBGL_debug_renderer_info');
  const name = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
  gl.getExtension('WEBGL_lose_context')?.loseContext();
  return String(name);
}

const frame = () => new Promise(r => requestAnimationFrame(() => r(null)));
const ms = (v: number) => Math.round(v * 10) / 10;

async function main(): Promise<void> {
  const t0 = performance.now();
  const q = new URLSearchParams(location.search);
  const init = window.__COVMAP_RENDER_INIT ?? {};
  const caseId = init.case ?? q.get('case');
  const width = Number(init.width ?? q.get('w') ?? 800);
  const height = Number(init.height ?? q.get('h') ?? 800);
  const out: RenderResult = {
    ready: false,
    error: null,
    case: caseId,
    organsDrawn: 0,
    organsListed: 0,
    triangles: 0,
    statusCounts: {},
    timings: {},
    renderer: webglRenderer(),
    devicePixelRatio: window.devicePixelRatio,
  };
  window.__render = out;
  const host = document.getElementById('map') as HTMLDivElement;
  host.style.width = `${width}px`;
  host.style.height = `${height}px`;
  try {
    if (!caseId) {
      throw new Error('no case given (?case= or __COVMAP_RENDER_INIT.case)');
    }
    const base = `/api/cases/${encodeURIComponent(caseId)}`;
    const [organsRes, meshes] = await Promise.all([fetch(`${base}/organs`), fetchMeshes(base)]);
    if (!organsRes.ok) {
      throw new Error(`organs: HTTP ${organsRes.status}`);
    }
    const organs = (await organsRes.json()).organs ?? [];
    const t1 = performance.now();

    const map = createOrganMap(host, { interactive: false, background: init.background });
    const loaded = await map.loadMeshes(meshes);
    map.setOrganInfo(organs);
    const statuses: Record<string, Status | OrganVisual> = {};
    for (const o of organs) {
      statuses[o.key] = 'skipped';
    }
    for (const o of init.coverage?.organs ?? []) {
      statuses[o.key] = { status: o.status ?? 'skipped', reader: o.reader ?? null, flagged: !!o.flagged };
    }
    Object.assign(statuses, init.statuses ?? {});
    map.setStatuses(statuses);
    const t2 = performance.now();
    map.render();
    await frame();
    await frame();
    const t3 = performance.now();

    out.organsDrawn = map.drawnCount();
    out.organsListed = organs.length;
    out.triangles = loaded.triangles;
    for (const v of Object.values(statuses)) {
      const s = typeof v === 'string' ? v : (v.status ?? 'skipped');
      out.statusCounts[s] = (out.statusCounts[s] ?? 0) + 1;
    }
    out.timings = {
      fetch: ms(t1 - t0),
      build: ms(t2 - t1),
      render: ms(t3 - t2),
      page_total: ms(t3 - t0),
    };
    out.ready = true;
  } catch (e) {
    out.error = (e as Error).message ?? String(e);
    (document.getElementById('err') as HTMLDivElement).textContent = out.error;
  }
}

main();
