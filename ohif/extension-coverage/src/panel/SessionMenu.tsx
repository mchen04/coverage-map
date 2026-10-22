import React, { useEffect, useRef, useState } from 'react';
import type CoverageService from '../CoverageService';
import type { CoverageState } from '../types';

interface Props {
  service: CoverageService;
  state: CoverageState;
  onClose: () => void;
}

function RulesForm({ service, state, onDone }: { service: CoverageService; state: CoverageState; onDone: () => void }) {
  const p = state.coverage?.params ?? { min_dwell_ms: 80, reviewed_threshold: 0.9, skipped_threshold: 0.05 };
  const [dwell, setDwell] = useState(String(p.min_dwell_ms));
  const [reviewed, setReviewed] = useState(String(Math.round(p.reviewed_threshold * 100)));
  const [skipped, setSkipped] = useState(String(Math.round(p.skipped_threshold * 100)));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const apply = async () => {
    setBusy(true);
    setErr(null);
    try {
      await service.applyRules({
        min_dwell_ms: Number(dwell),
        reviewed_threshold: Number(reviewed) / 100,
        skipped_threshold: Number(skipped) / 100,
      });
      onDone();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const field = (label: string, value: string, set: (v: string) => void, id: string) => (
    <>
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        data-testid={id}
        type="number"
        value={value}
        onChange={e => set(e.target.value)}
        onKeyDown={e => e.stopPropagation()}
      />
    </>
  );
  return (
    <div data-testid="covmap-rules-form">
      <div className="cm-pop-title">Research reading rules</div>
      <div className="cm-form">
        {field('Min dwell per voxel (ms)', dwell, setDwell, 'covmap-rules-dwell')}
        {field('Reviewed at ≥ (% voxels)', reviewed, setReviewed, 'covmap-rules-reviewed')}
        {field('Skipped below (% voxels)', skipped, setSkipped, 'covmap-rules-skipped')}
        <span className="cm-muted">Re-scores this whole session.</span>
        <button
          className="cm-btn cm-btn--primary"
          data-testid="covmap-rules-apply"
          disabled={busy}
          onClick={apply}
        >
          Apply
        </button>
      </div>
      {err && <div className="cm-msg cm-msg--error">{err}</div>}
    </div>
  );
}

export default function SessionMenu({ service, state, onClose }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [view, setView] = useState<'main' | 'samples' | 'rules'>('main');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      const t = e.target as HTMLElement;
      if (ref.current && !ref.current.contains(t) && !t.closest('[data-testid="covmap-session-menu"]')) {
        onClose();
      }
    };
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    document.addEventListener('pointerdown', onDown, true);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('pointerdown', onDown, true);
      document.removeEventListener('keydown', onKey);
    };
  }, [onClose]);

  const run = (fn: () => Promise<void> | void) => async () => {
    setBusy(true);
    try {
      await fn();
      onClose();
    } catch (e) {
      console.warn('covmap:', e);
    } finally {
      setBusy(false);
    }
  };
  const live = !!state.sessionId;

  return (
    <div className="cm-pop" ref={ref} data-testid="covmap-session-popover">
      {view === 'rules' && <RulesForm service={service} state={state} onDone={onClose} />}
      {view === 'samples' && (
        <div data-testid="covmap-sample-list">
          <div className="cm-pop-title">Load a sample reading</div>
          {state.samples.length === 0 && <div className="cm-msg">No sample readings for this case.</div>}
          {state.samples.map(s => (
            <button
              key={s.name}
              className="cm-sample"
              data-testid={`covmap-sample-${s.name}`}
              disabled={busy}
              title={s.description ?? ''}
              onClick={run(() => service.loadSample(s.name))}
            >
              {s.label}
              <small>
                source: {s.source ?? 'unknown'}
                {typeof s.events === 'number' ? ` · ${s.events} events` : ''}
              </small>
            </button>
          ))}
          <div className="cm-pop-sep" />
          <button className="cm-pop-item" onClick={() => setView('main')}>
            ‹ Back
          </button>
        </div>
      )}
      {view === 'main' && (
        <>
          <button
            className="cm-pop-item"
            data-testid="covmap-new-reading"
            disabled={busy || !state.caseId}
            onClick={run(() => service.newReading())}
          >
            New reading
          </button>
          <button
            className="cm-pop-item"
            data-testid="covmap-load-sample"
            disabled={!state.caseId}
            onClick={() => setView('samples')}
          >
            Load sample reading… <span className="cm-muted">({state.samples.length})</span>
          </button>
          <button
            className="cm-pop-item"
            data-testid="covmap-replay"
            disabled={!live || busy}
            onClick={run(() => service.startReplay())}
          >
            Replay this session
          </button>
          <button
            className="cm-pop-item"
            data-testid="covmap-rules"
            disabled={!live}
            onClick={() => setView('rules')}
          >
            Research reading rules…
          </button>
          <div className="cm-pop-sep" />
          <div className="cm-pop-title">Export this session</div>
          <div className="cm-links" data-testid="covmap-export">
            {(['jsonl', 'json', 'csv'] as const).map(f => {
              const href = service.exportUrl(f);
              return href ? (
                <a
                  key={f}
                  href={href}
                  download
                  data-testid={`covmap-export-${f}`}
                  onClick={e => {
                    // include the events still queued in this tab
                    e.preventDefault();
                    service.flush().finally(() => {
                      const a = document.createElement('a');
                      a.href = href;
                      a.download = '';
                      a.click();
                    });
                  }}
                >
                  {f === 'jsonl' ? 'events (JSONL)' : f === 'json' ? 'coverage (JSON)' : 'organs (CSV)'}
                </a>
              ) : null;
            })}
          </div>
        </>
      )}
    </div>
  );
}
