import React, { useEffect, useRef, useState } from 'react';
import {
  ATTESTED_COLOR,
  STATUS_COLORS,
  createOrganMap,
  type MapView,
  type OrganMap,
  type OrganVisual,
  type Orientation,
} from '@covmap/organ-map';
import type CoverageService from '../CoverageService';
import { pct, STATUS_WORD, type Row } from './model';

interface Props {
  service: CoverageService;
  caseId: string | null;
  rows: Row[];
  highlight: string | null;
  onHover: (key: string | null) => void;
  replaying: boolean;
  /** kept mounted (camera and GL context survive) but not shown, e.g. under the finish sheet */
  hidden?: boolean;
}

const VIEW_BUTTONS: Array<[MapView, string, string]> = [
  ['front', 'A', 'Front view (anterior), fit the whole model'],
  ['back', 'P', 'Back view (posterior)'],
  ['right', 'R', "View from the patient's right side"],
  ['left', 'L', "View from the patient's left side"],
];

const NO_ORIENT: Orientation = { left: 'R', right: 'L', top: 'S', bottom: 'I' };

/** Not a gap: displayed enough to count, or marked done / N/A by the reader. */
function settled(r: Row): boolean {
  return r.status === 'reviewed' || r.layer.reader === 'done' || r.layer.reader === 'na';
}

export default function OrganMapView({ service, caseId, rows, highlight, onHover, replaying, hidden }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<OrganMap | null>(null);
  const rowsRef = useRef<Row[]>(rows);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const [orient, setOrient] = useState<Orientation>(NO_ORIENT);
  const [view, setView] = useState<MapView | null>('front');
  const [gapsOnly, setGapsOnly] = useState(false);
  const [visible, setVisible] = useState(0);
  rowsRef.current = rows;

  useEffect(() => {
    const host = hostRef.current;
    if (!host || !caseId) {
      return;
    }
    let cancelled = false;
    setReady(false);
    setError(null);
    let map: OrganMap;
    try {
      map = createOrganMap(host, { background: [0.027, 0.039, 0.078] });
    } catch (e) {
      setError(`3D map unavailable: ${(e as Error).message}`);
      return;
    }
    mapRef.current = map;
    const ro = new ResizeObserver(() => map.resize());
    ro.observe(host);
    const unsubPick = map.onPick(e => {
      if (e.kind === 'hover') {
        setHover(e.key);
        onHover(e.key);
      } else if (e.key && !service.state.replay) {
        service.jumpToOrgan(e.key);
      }
    });
    const unsubCam = map.onCamera(o => setOrient(o));
    // a drag on the canvas leaves the canonical view
    const onDrag = (ev: PointerEvent) => ev.buttons && setView(null);
    const onWheel = () => setView(null);
    host.addEventListener('pointermove', onDrag);
    host.addEventListener('wheel', onWheel, { passive: true });
    service
      .meshes(caseId)
      .then(data => (cancelled ? null : map.loadMeshes(data)))
      .then(res => {
        if (!res || cancelled) {
          return;
        }
        map.setOrganInfo(rowsRef.current.map(r => r.organ));
        setOrient(map.orientation());
        setReady(true);
        requestAnimationFrame(() => service.markMapReady());
      })
      .catch(e => !cancelled && setError(`Could not load organ meshes: ${(e as Error).message}`));
    return () => {
      cancelled = true;
      ro.disconnect();
      unsubPick();
      unsubCam();
      host.removeEventListener('pointermove', onDrag);
      host.removeEventListener('wheel', onWheel);
      map.dispose();
      mapRef.current = null;
    };
  }, [caseId, service]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) {
      return;
    }
    const visuals: Record<string, OrganVisual> = {};
    rows.forEach(r => {
      visuals[r.organ.key] = { status: r.status, reader: r.layer.reader, flagged: r.layer.flagged };
    });
    map.setStatuses(visuals);
    map.setHidden(gapsOnly ? rows.filter(settled).map(r => r.organ.key) : []);
    setVisible(map.visibleCount());
  }, [rows, ready, gapsOnly]);

  useEffect(() => {
    if (ready) {
      mapRef.current?.setHighlight(hover ?? highlight);
    }
  }, [hover, highlight, ready]);

  const showView = (v: MapView) => {
    mapRef.current?.resetCamera(v);
    setView(v);
  };

  const hoverRow = hover ? rows.find(r => r.organ.key === hover) : null;
  return (
    <div className="cm-map-wrap" style={hidden ? { display: 'none' } : undefined}>
      <div
        className="cm-map"
        data-testid="covmap-map"
        data-ready={ready ? '1' : '0'}
        data-organs={ready ? String(mapRef.current?.drawnCount() ?? 0) : '0'}
        data-visible={ready ? String(visible) : '0'}
        data-view={view ?? ''}
        data-orient={`${orient.left ?? '-'}${orient.right ?? '-'}`}
      >
        <div ref={hostRef} className="cm-map-canvas" style={{ cursor: hover ? 'pointer' : 'grab' }} />
        {!caseId && <div className="cm-map-msg">The 3D organ map appears once the case is identified.</div>}
        {caseId && !ready && !error && <div className="cm-map-msg">Loading organ meshes…</div>}
        {error && <div className="cm-map-msg cm-msg--error">{error}</div>}
        {hoverRow && (
          <div className="cm-map-hover" data-testid="covmap-map-hover" data-key={hoverRow.organ.key}>
            <b>{hoverRow.organ.name}</b> · {pct(hoverRow.coverage)} · {STATUS_WORD[hoverRow.status]}
            {hoverRow.layer.reader === 'done' ? ' · marked done' : ''}
            {hoverRow.layer.reader === 'na' ? ' · N/A' : ''}
            {!replaying && <span className="cm-muted"> · click to jump</span>}
          </div>
        )}
        <div className="cm-map-tools" role="toolbar" aria-label="3D map view">
          {VIEW_BUTTONS.map(([v, letter, title]) => (
            <button
              key={v}
              className={`cm-btn${view === v ? ' cm-btn--on' : ''}`}
              data-testid={`covmap-map-view-${v}`}
              title={title}
              aria-label={title}
              aria-pressed={view === v}
              onClick={() => showView(v)}
            >
              {letter}
            </button>
          ))}
          <button
            className={`cm-btn${gapsOnly ? ' cm-btn--on' : ''}`}
            data-testid="covmap-map-gaps-only"
            title="Hide organs that are displayed enough to count, marked done or N/A, so the rest (e.g. the spine) can be reached"
            aria-pressed={gapsOnly}
            onClick={() => setGapsOnly(g => !g)}
          >
            Hide reviewed
          </button>
        </div>
        {orient.left && (
          <span className="cm-orient" style={{ left: 4 }} data-testid="covmap-map-orient-left">
            {orient.left}
          </span>
        )}
        {orient.right && (
          <span className="cm-orient" style={{ right: 4 }} data-testid="covmap-map-orient-right">
            {orient.right}
          </span>
        )}
      </div>
      <div className="cm-legend" data-testid="covmap-map-legend">
        <span>
          <i className="cm-swatch" style={{ background: STATUS_COLORS.reviewed }} />
          reviewed
        </span>
        <span>
          <i className="cm-swatch" style={{ background: STATUS_COLORS.partial }} />
          partial
        </span>
        <span>
          <i className="cm-swatch" style={{ background: STATUS_COLORS.skipped }} />
          skipped
        </span>
        <span>
          <i className="cm-swatch" style={{ background: ATTESTED_COLOR }} />
          marked done
        </span>
        <span title="Not applicable: drawn as a faint violet wireframe">
          <i className="cm-swatch cm-swatch--na" />
          N/A
        </span>
      </div>
    </div>
  );
}
