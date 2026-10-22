import React, { useEffect, useRef, useState } from 'react';
import type CoverageService from '../CoverageService';
import { DEFAULT_RULES, sourceText } from '../presets';
import type { CaseInfo, CoverageState, Rules } from '../types';

export type MenuView = 'main' | 'samples' | 'rules' | 'cases' | 'confirm-new';

interface Props {
  service: CoverageService;
  state: CoverageState;
  initial?: MenuView;
  onClose: () => void;
}

export function isCustomRules(p: Rules | undefined | null): boolean {
  if (!p) {
    return false;
  }
  return (
    Math.abs(p.min_dwell_ms - DEFAULT_RULES.min_dwell_ms) > 1e-9 ||
    Math.abs(p.reviewed_threshold - DEFAULT_RULES.reviewed_threshold) > 1e-9 ||
    Math.abs(p.skipped_threshold - DEFAULT_RULES.skipped_threshold) > 1e-9
  );
}

/** Parse the three fields; an error message when a field is empty or out of range. */
function parseRules(dwell: string, reviewed: string, skipped: string): Rules | string {
  const num = (v: string) => (v.trim() === '' ? NaN : Number(v));
  const d = num(dwell);
  const r = num(reviewed);
  const s = num(skipped);
  if (!Number.isFinite(d) || d < 0) {
    return 'Min dwell must be a number of milliseconds, 0 or more.';
  }
  if (!Number.isFinite(r) || r <= 0 || r > 100) {
    return 'Reviewed threshold must be a percentage above 0 and at most 100.';
  }
  if (!Number.isFinite(s) || s < 0 || s >= r) {
    return 'Skipped threshold must be a percentage from 0 up to below the reviewed threshold.';
  }
  return { min_dwell_ms: d, reviewed_threshold: r / 100, skipped_threshold: s / 100 };
}

function RulesForm({ service, state, onDone }: { service: CoverageService; state: CoverageState; onDone: () => void }) {
  const p = state.coverage?.params ?? DEFAULT_RULES;
  const [dwell, setDwell] = useState(String(p.min_dwell_ms));
  const [reviewed, setReviewed] = useState(String(Math.round(p.reviewed_threshold * 100)));
  const [skipped, setSkipped] = useState(String(Math.round(p.skipped_threshold * 100)));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async (rules: Rules) => {
    setBusy(true);
    setErr(null);
    try {
      await service.applyRules(rules);
      onDone();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const apply = () => {
    const rules = parseRules(dwell, reviewed, skipped);
    if (typeof rules === 'string') {
      setErr(rules);
      return;
    }
    submit(rules);
  };
  const field = (label: string, value: string, set: (v: string) => void, id: string, max?: number) => (
    <>
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        data-testid={id}
        type="number"
        min={0}
        max={max}
        required
        value={value}
        onChange={e => set(e.target.value)}
        onKeyDown={e => {
          e.stopPropagation();
          if (e.key === 'Enter') {
            apply();
          }
        }}
      />
    </>
  );
  return (
    <div data-testid="covmap-rules-form">
      <div className="cm-pop-title">Research reading rules</div>
      <div className="cm-form">
        {field('Min dwell per voxel (ms)', dwell, setDwell, 'covmap-rules-dwell')}
        {field('Reviewed at ≥ (% voxels)', reviewed, setReviewed, 'covmap-rules-reviewed', 100)}
        {field('Skipped below (% voxels)', skipped, setSkipped, 'covmap-rules-skipped', 100)}
        <span className="cm-muted">Re-scores this whole session.</span>
        <button className="cm-btn cm-btn--primary" data-testid="covmap-rules-apply" disabled={busy} onClick={apply}>
          Apply
        </button>
      </div>
      <div className="cm-row-flex" style={{ padding: '0 6px 4px' }}>
        <span className="cm-muted cm-grow">
          Defaults: {DEFAULT_RULES.min_dwell_ms} ms, {DEFAULT_RULES.reviewed_threshold * 100}%,{' '}
          {DEFAULT_RULES.skipped_threshold * 100}%
        </span>
        <button
          className="cm-btn"
          data-testid="covmap-rules-restore"
          disabled={busy || !isCustomRules(state.coverage?.params)}
          onClick={() => submit({ ...DEFAULT_RULES })}
        >
          Restore defaults
        </button>
      </div>
      {err && (
        <div className="cm-msg cm-msg--error" data-testid="covmap-rules-error">
          {err}
        </div>
      )}
    </div>
  );
}

function CaseList({ service, state }: { service: CoverageService; state: CoverageState }) {
  const [cases, setCases] = useState<CaseInfo[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    service
      .listCases()
      .then(setCases)
      .catch(e => setErr((e as Error).message));
  }, [service]);
  return (
    <div data-testid="covmap-case-list">
      <div className="cm-pop-title">Open a case</div>
      {err && <div className="cm-msg cm-msg--error">{err}</div>}
      {!cases && !err && <div className="cm-msg">Loading…</div>}
      {cases?.map(c => {
        const current = c.case === state.caseId;
        return (
          <button
            key={c.case}
            className="cm-sample"
            data-testid={`covmap-case-${c.case}`}
            disabled={current || !c.segmented}
            onClick={() => {
              window.location.assign(`/coverage?StudyInstanceUIDs=${encodeURIComponent(c.study_uid)}`);
            }}
          >
            {c.case}
            {current ? ' (open)' : ''}
            <small>
              {c.n_slices} images · {c.organs} organs{c.segmented ? '' : ' · not segmented'}
            </small>
          </button>
        );
      })}
      <div className="cm-muted" style={{ padding: '2px 6px', fontSize: 10 }}>
        Your reading of this case is kept and resumes when you come back to it.
      </div>
    </div>
  );
}

export default function SessionMenu({ service, state, initial = 'main', onClose }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [view, setView] = useState<MenuView>(initial);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<{ title: string; ok: string; run: () => Promise<unknown> } | null>(
    initial === 'confirm-new'
      ? { title: 'Start a new reading?', ok: 'Start new reading', run: () => service.newReading() }
      : null
  );

  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      const t = e.target as HTMLElement;
      if (ref.current && !ref.current.contains(t) && !t.closest('[data-covmap-menu-toggle]')) {
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

  const run = (fn: () => Promise<unknown> | void) => async () => {
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
  /** Ask first when the current reading has something in it; it cannot be reopened later. */
  const guarded = (title: string, ok: string, fn: () => Promise<unknown>) => () => {
    if (service.hasReading()) {
      setConfirm({ title, ok, run: fn });
      setView('confirm-new');
    } else {
      run(fn)();
    }
  };
  const live = !!state.sessionId;

  return (
    <div className="cm-pop" ref={ref} data-testid="covmap-session-popover">
      {view === 'confirm-new' && confirm && (
        <div data-testid="covmap-confirm" role="alertdialog" aria-label={confirm.title}>
          <div className="cm-pop-title">{confirm.title}</div>
          <div className="cm-pop-text">
            The current reading ends here. It stays on the server (export it first if you need the
            files), but this page will not return to it.
          </div>
          <div className="cm-row-flex" style={{ justifyContent: 'flex-end', padding: '2px 6px 4px' }}>
            <button className="cm-btn" data-testid="covmap-confirm-cancel" onClick={() => setView('main')}>
              Cancel
            </button>
            <button
              className="cm-btn cm-btn--primary"
              data-testid="covmap-confirm-ok"
              disabled={busy}
              onClick={run(confirm.run)}
            >
              {confirm.ok}
            </button>
          </div>
        </div>
      )}
      {view === 'rules' && <RulesForm service={service} state={state} onDone={onClose} />}
      {view === 'cases' && <CaseList service={service} state={state} />}
      {view === 'samples' && (
        <div data-testid="covmap-sample-list">
          <div className="cm-pop-title">Start from a sample reading</div>
          {state.samples.length === 0 && <div className="cm-msg">No sample readings for this case.</div>}
          {state.samples.map(s => (
            <button
              key={s.name}
              className="cm-sample"
              data-testid={`covmap-sample-${s.name}`}
              disabled={busy}
              title={s.description ?? ''}
              onClick={guarded('Start from this sample?', 'Start from sample', () => service.loadSample(s.name))}
            >
              {s.label.replace(/^sample\s*:?\s*/i, '')}
              <small>
                {sourceText(s.source)}
                {typeof s.events === 'number' ? ` · ${s.events} events` : ''}
              </small>
            </button>
          ))}
          <div className="cm-muted" style={{ padding: '2px 6px', fontSize: 10 }}>
            Starts a new reading with the sample&apos;s events; what you read is added on top.
          </div>
        </div>
      )}
      {view !== 'main' && view !== 'confirm-new' && (
        <>
          <div className="cm-pop-sep" />
          <button className="cm-pop-item" onClick={() => setView('main')}>
            ‹ Back
          </button>
        </>
      )}
      {view === 'main' && (
        <>
          <button
            className="cm-pop-item"
            data-testid="covmap-new-reading"
            disabled={busy || !state.caseId}
            onClick={guarded('Start a new reading?', 'Start new reading', () => service.newReading())}
          >
            New reading
          </button>
          <button
            className="cm-pop-item"
            data-testid="covmap-load-sample"
            disabled={!state.caseId}
            onClick={() => setView('samples')}
          >
            Start from a sample reading… <span className="cm-muted">({state.samples.length})</span>
          </button>
          <button className="cm-pop-item" data-testid="covmap-open-case" onClick={() => setView('cases')}>
            Open another case…
          </button>
          <button
            className="cm-pop-item"
            data-testid="covmap-replay"
            disabled={!live || busy}
            onClick={run(() => service.startReplay())}
          >
            Replay this session <span className="cm-muted">(pauses recording)</span>
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
