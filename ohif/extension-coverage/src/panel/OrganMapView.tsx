import React, { useEffect, useRef, useState } from 'react';
import {
  ATTESTED_COLOR,
  NA_COLOR,
  STATUS_COLORS,
  createOrganMap,
  fetchMeshes,
  type OrganMap,
  type OrganVisual,
} from '@covmap/organ-map';
import type CoverageService from '../CoverageService';
import { pct, STATUS_WORD, type Row } from './model';

const meshCache = new Map<string, ReturnType<typeof fetchMeshes>>();

function meshesFor(base: string) {
  let p = meshCache.get(base);
  if (!p) {
    p = fetchMeshes(base);
    p.catch(() => meshCache.delete(base));
    meshCache.set(base, p);
  }
  return p;
}

interface Props {
  service: CoverageService;
  caseId: string | null;
  rows: Row[];
  highlight: string | null;
  onHover: (key: string | null) => void;
  replaying: boolean;
}

export default function OrganMapView({ service, caseId, rows, highlight, onHover, replaying }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<OrganMap | null>(null);
  const rowsRef = useRef<Row[]>(rows);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hover, setHover] = useState<string | null>(null);
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
    meshesFor(service.api.meshBase(caseId))
      .then(data => (cancelled ? null : map.loadMeshes(data)))
      .then(res => {
        if (!res || cancelled) {
          return;
        }
        map.setOrganInfo(rowsRef.current.map(r => r.organ));
        setReady(true);
        requestAnimationFrame(() => service.markMapReady());
      })
      .catch(e => !cancelled && setError(`Could not load organ meshes: ${(e as Error).message}`));
    return () => {
      cancelled = true;
      ro.disconnect();
      unsubPick();
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
  }, [rows, ready]);

  useEffect(() => {
    if (ready) {
      mapRef.current?.setHighlight(hover ?? highlight);
    }
  }, [hover, highlight, ready]);

  const hoverRow = hover ? rows.find(r => r.organ.key === hover) : null;
  return (
    <div
      className="cm-map"
      data-testid="covmap-map"
      data-ready={ready ? '1' : '0'}
      data-organs={ready ? String(mapRef.current?.drawnCount() ?? 0) : '0'}
    >
      <div
        ref={hostRef}
        className="cm-map-canvas"
        style={{ cursor: hover ? 'pointer' : 'grab' }}
      />
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
      <div className="cm-map-tools">
        <button
          className="cm-btn"
          title="Reset view: anterior, head up, patient right on screen left"
          onClick={() => mapRef.current?.resetCamera()}
        >
          Reset
        </button>
      </div>
      <div className="cm-legend">
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
        <span>
          <i className="cm-swatch" style={{ background: NA_COLOR }} />
          N/A
        </span>
      </div>
      <span className="cm-orient" style={{ left: 4 }} title="patient right">
        R
      </span>
      <span className="cm-orient" style={{ right: 4 }} title="patient left">
        L
      </span>
    </div>
  );
}
