/**
 * Selected-organ outline drawn as SVG over the Cornerstone viewport. Contours come from
 * GET /api/cases/{case}/outline?organ=&k= in image-pixel (r, c) on acquisition slice k,
 * go through the volume affine (c, r, k, 1) -> LPS and then viewport.worldToCanvas, and
 * are redrawn after every render (slice, pan, zoom, window). Only drawn when the
 * viewport shows an acquisition slice of the case series.
 */
import { STATUS_COLORS } from '@covmap/organ-map';
import { allViewports, isPlanarVolume, isStack } from './instrumentation';
import type CoverageService from './CoverageService';

type Contours = [number, number][][];

const SVG_NS = 'http://www.w3.org/2000/svg';
const LAYER_CLASS = 'covmap-outline-layer';
const CACHE_MAX = 300;

export class OutlineOverlay {
  private cache = new Map<string, Promise<Contours>>();
  private layers = new Map<string, SVGSVGElement>();
  private scheduled = new Set<string>();
  private unsubs: Array<() => void> = [];

  constructor(private readonly service: CoverageService) {}

  start(): void {
    const { unsubscribe } = this.service.subscribe(
      this.service.EVENTS.STATE_CHANGED,
      () => this.redrawAll()
    );
    this.unsubs.push(unsubscribe, this.service.onViewportRendered(id => this.schedule(id)));
  }

  stop(): void {
    this.unsubs.forEach(u => u());
    this.unsubs = [];
    this.layers.forEach(svg => svg.remove());
    this.layers.clear();
    this.cache.clear();
  }

  private redrawAll(): void {
    const ids = new Set([...allViewports().map(vp => vp.id), ...this.layers.keys()]);
    ids.forEach(id => this.schedule(id));
  }

  private schedule(viewportId: string): void {
    if (this.scheduled.has(viewportId)) {
      return;
    }
    this.scheduled.add(viewportId);
    requestAnimationFrame(() => {
      this.scheduled.delete(viewportId);
      this.draw(viewportId);
    });
  }

  private contours(caseId: string, organ: string, k: number): Promise<Contours> {
    const key = `${caseId}|${organ}|${k}`;
    let p = this.cache.get(key);
    if (!p) {
      p = this.service.api
        .outline(caseId, organ, k)
        .then(r => r.contours as Contours)
        .catch(e => {
          this.cache.delete(key);
          throw e;
        });
      if (this.cache.size >= CACHE_MAX) {
        this.cache.delete(this.cache.keys().next().value);
      }
      this.cache.set(key, p);
    }
    return p;
  }

  private clear(viewportId: string): void {
    this.layers.get(viewportId)?.remove();
    this.layers.delete(viewportId);
  }

  private layerFor(vp: any): SVGSVGElement | null {
    const host: HTMLElement | null = vp.canvas?.parentElement ?? null;
    if (!host) {
      return null;
    }
    let svg = this.layers.get(vp.id);
    if (!svg || svg.parentElement !== host) {
      svg?.remove();
      svg = document.createElementNS(SVG_NS, 'svg') as SVGSVGElement;
      svg.setAttribute('class', LAYER_CLASS);
      svg.setAttribute('data-testid', 'covmap-outline');
      Object.assign(svg.style, {
        position: 'absolute',
        left: '0',
        top: '0',
        width: '100%',
        height: '100%',
        pointerEvents: 'none',
        zIndex: '5',
      });
      if (getComputedStyle(host).position === 'static') {
        host.style.position = 'relative';
      }
      host.appendChild(svg);
      this.layers.set(vp.id, svg);
    }
    return svg;
  }

  private async draw(viewportId: string): Promise<void> {
    const { state } = this.service;
    const vp = allViewports().find(v => v.id === viewportId);
    const organKey = state.selected;
    const volume = this.service.getVolume();
    if (!vp || !state.outline || !organKey || !state.caseId || !volume) {
      this.clear(viewportId);
      return;
    }
    if (!isStack(vp) && !isPlanarVolume(vp)) {
      this.clear(viewportId);
      return;
    }
    const k = this.service.sliceIndexOf(viewportId);
    if (k === null) {
      this.clear(viewportId);
      return;
    }
    let contours: Contours;
    try {
      contours = await this.contours(state.caseId, organKey, k);
    } catch (e) {
      console.warn('covmap: outline fetch failed', e);
      this.clear(viewportId);
      return;
    }
    // A newer render may have moved on while the request was in flight.
    const now = this.service.state;
    if (!now.outline || now.selected !== organKey || this.service.sliceIndexOf(viewportId) !== k) {
      return;
    }
    const svg = this.layerFor(vp);
    if (!svg) {
      return;
    }
    const A = volume.affine_lps;
    const toCanvas = ([r, c]: [number, number]) => {
      const world = [0, 1, 2].map(i => A[i][0] * c + A[i][1] * r + A[i][2] * k + A[i][3]);
      const p = vp.worldToCanvas(world);
      return `${p[0].toFixed(1)},${p[1].toFixed(1)}`;
    };
    const d = contours
      .filter(c => c.length > 2)
      .map(c => `M${c.map(toCanvas).join('L')}Z`)
      .join('');
    const status = now.coverage?.organs?.find(o => o.key === organKey)?.status ?? 'skipped';
    const color = STATUS_COLORS[status];
    const name = now.organs.find(o => o.key === organKey)?.name ?? organKey;
    const label = d ? name : `${name}: not on this slice`;
    svg.innerHTML = '';
    if (d) {
      const halo = document.createElementNS(SVG_NS, 'path');
      halo.setAttribute('d', d);
      halo.setAttribute('fill', 'none');
      halo.setAttribute('stroke', 'rgba(0,0,0,0.75)');
      halo.setAttribute('stroke-width', '3.5');
      halo.setAttribute('stroke-linejoin', 'round');
      const path = document.createElementNS(SVG_NS, 'path');
      path.setAttribute('d', d);
      path.setAttribute('fill', color);
      path.setAttribute('fill-opacity', '0.08');
      path.setAttribute('stroke', color);
      path.setAttribute('stroke-width', '1.6');
      path.setAttribute('stroke-linejoin', 'round');
      svg.append(halo, path);
    }
    const text = document.createElementNS(SVG_NS, 'text');
    text.setAttribute('x', '50%');
    text.setAttribute('y', '18');
    text.setAttribute('text-anchor', 'middle');
    text.setAttribute('fill', color);
    text.setAttribute('font-size', '12');
    text.setAttribute('paint-order', 'stroke');
    text.setAttribute('stroke', 'rgba(0,0,0,0.8)');
    text.setAttribute('stroke-width', '3');
    text.textContent = `Outline: ${label} (O to hide)`;
    svg.append(text);
  }
}
