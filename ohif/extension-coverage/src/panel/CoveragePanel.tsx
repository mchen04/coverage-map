import React, { useCallback, useMemo, useState } from 'react';
import type CoverageService from '../CoverageService';
import { PRESETS } from '../presets';
import FinishSheet from './FinishSheet';
import OrganList from './OrganList';
import OrganMapView from './OrganMapView';
import ReplayBar from './ReplayBar';
import SessionMenu from './SessionMenu';
import { buildRows, summarize, useCoverageState } from './model';
import './coverage.css';

const SHORTCUTS: Array<[string, string]> = [
  ['N', 'next gap'],
  ['D', 'done'],
  ['A', 'N/A'],
  ['G', 'flag'],
  ['O', 'outline'],
  ['F', 'finish'],
  ['1', 'soft'],
  ['2', 'lung'],
  ['3', 'bone'],
  ['4', 'liver'],
  ['5', 'brain'],
];

export default function CoveragePanel({ servicesManager }: { servicesManager: AppTypes.ServicesManager }) {
  const service = (servicesManager.services as any).coverageService as CoverageService;
  const state = useCoverageState(service);
  const [menuOpen, setMenuOpen] = useState(false);
  const [hot, setHot] = useState<string | null>(null);
  const rows = useMemo(() => buildRows(service, state), [service, state]);
  const sum = summarize(rows, state);
  const finished = service.finished();
  const replaying = !!state.replay;
  const closeMenu = useCallback(() => setMenuOpen(false), []);
  const ready = state.phase === 'ready';

  let message: React.ReactNode = null;
  if (state.phase === 'idle' || state.phase === 'waiting') {
    message = 'Waiting for the first displayed image…';
  } else if (state.phase === 'lookup') {
    message = 'Identifying the case…';
  } else if (state.phase === 'session') {
    message = 'Starting the reading session…';
  } else if (state.phase === 'no-case' || state.phase === 'error') {
    message = (
      <span className="cm-msg--error">
        {state.error}{' '}
        <button className="cm-btn" onClick={() => service.retry()}>
          Retry
        </button>
      </span>
    );
  }

  return (
    <div className="cm-root" data-testid="covmap-panel" data-phase={state.phase}>
      <div className="cm-summary" data-testid="covmap-summary" data-finished={finished?.confirmed ? '1' : '0'}>
        <span className="cm-count" title="reviewed (measured)">
          <i className="cm-dot cm-dot--reviewed" />
          {sum.reviewed}
        </span>
        <span className="cm-count" title="partial">
          <i className="cm-dot cm-dot--partial" />
          {sum.partial}
        </span>
        <span className="cm-count" title="skipped">
          <i className="cm-dot cm-dot--skipped" />
          {sum.skipped}
        </span>
        <span className="cm-muted">·</span>
        <span title="not reviewed and not marked done or N/A" data-testid="covmap-remaining">
          <b>{sum.remaining}</b> left
        </span>
        <span className="cm-muted" title="share of all organ voxels displayed under a qualifying window">
          · {sum.voxelPct === null ? '–' : `${sum.voxelPct}%`} vox
        </span>
        <span className="cm-grow" />
        {finished?.confirmed && !replaying && (
          <span className="cm-chip cm-chip--done" data-testid="covmap-finished" title="Reader confirmed finish">
            ✓ Finished
          </span>
        )}
        <button
          className="cm-btn"
          data-testid="covmap-session-menu"
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen(o => !o)}
        >
          Session ▾
        </button>
      </div>
      <div className="cm-sub">
        <span className="cm-ellipsis cm-grow" data-testid="covmap-session-label">
          {replaying ? 'Replay (read-only): ' : ''}
          {state.sessionLabel || 'No session yet'}
          {state.sessionSource && state.sessionSource !== 'ohif' && (
            <span className="cm-chip cm-chip--sample" style={{ marginLeft: 4 }}>
              {state.sessionSource}
            </span>
          )}
        </span>
        {(sum.attested > 0 || sum.na > 0 || sum.flagged > 0) && (
          <span>
            {sum.attested ? `${sum.attested} marked done` : ''}
            {sum.attested && (sum.na || sum.flagged) ? ' · ' : ''}
            {sum.na ? `${sum.na} N/A` : ''}
            {sum.na && sum.flagged ? ' · ' : ''}
            {sum.flagged ? `${sum.flagged} flagged` : ''}
          </span>
        )}
        <span title="events sent to the coverage server">{state.eventsSent} ev</span>
      </div>
      {menuOpen && <SessionMenu service={service} state={state} onClose={closeMenu} />}

      {message && <div className="cm-msg">{message}</div>}
      {state.caseId && (
        <OrganMapView
          service={service}
          caseId={state.caseId}
          rows={rows}
          highlight={hot ?? state.selected}
          onHover={setHot}
          replaying={replaying}
        />
      )}
      {state.caseId &&
        (state.replay ? (
          <ReplayBar service={service} replay={state.replay} />
        ) : (
          <div className="cm-actions">
            <button
              className="cm-btn cm-btn--primary"
              data-testid="covmap-next-gap"
              disabled={!ready}
              title="Jump to the next organ that is not reviewed, with its window (N)"
              onClick={() => service.nextGap()}
            >
              Next gap <span className="cm-kbd">N</span>
            </button>
            {(['soft', 'lung', 'bone'] as const).map(id => (
              <button
                key={id}
                className="cm-btn"
                data-testid={`covmap-window-${id}`}
                title={`${PRESETS[id].label} window ${PRESETS[id].ww}/${PRESETS[id].wc} (${PRESETS[id].key})`}
                onClick={() => service.applyPreset(PRESETS[id])}
              >
                {PRESETS[id].short} <span className="cm-kbd">{PRESETS[id].key}</span>
              </button>
            ))}
            <button
              className="cm-btn"
              data-testid="covmap-finish"
              disabled={!ready}
              title="Review what is left and finish (F)"
              onClick={() => service.openFinish()}
            >
              Finish <span className="cm-kbd">F</span>
            </button>
          </div>
        ))}
      {state.caseId && (
        <OrganList
          service={service}
          rows={rows}
          selected={state.selected}
          hot={hot}
          replaying={replaying}
          onHover={setHot}
        />
      )}
      {state.finishOpen && !replaying && <FinishSheet service={service} rows={rows} />}
      <div className="cm-foot" data-testid="covmap-shortcuts">
        {SHORTCUTS.map(([k, label]) => (
          <span key={k} style={{ marginRight: 5, whiteSpace: 'nowrap' }}>
            <span className="cm-kbd">{k}</span>
            {label}
          </span>
        ))}
        <div className="cm-note" data-testid="covmap-note">
          Research use only, not a medical device. Coverage measures what was displayed (slice, region,
          window, dwell), not what was perceived.
        </div>
      </div>
    </div>
  );
}
