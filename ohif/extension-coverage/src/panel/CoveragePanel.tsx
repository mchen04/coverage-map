import React, { useCallback, useMemo, useState } from 'react';
import type CoverageService from '../CoverageService';
import { classifyWindow, PRESETS, presetOf, sourceText } from '../presets';
import FinishSheet from './FinishSheet';
import OrganList from './OrganList';
import OrganMapView from './OrganMapView';
import ReplayBar from './ReplayBar';
import SessionMenu, { isCustomRules, type MenuView } from './SessionMenu';
import { buildRows, summarize, useCoverageState } from './model';
import './coverage.css';

/** localStorage key: the reader closed the first-use hint (kept across reloads and cases). */
const FIRST_USE_KEY = 'covmap:first-use-hint';

function firstUseDismissed(): boolean {
  try {
    return localStorage.getItem(FIRST_USE_KEY) === 'dismissed';
  } catch {
    return false; // storage unavailable: show it
  }
}

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
  const [menu, setMenu] = useState<MenuView | null>(null);
  const [hot, setHot] = useState<string | null>(null);
  const [firstUse, setFirstUse] = useState(() => !firstUseDismissed());
  const dismissFirstUse = useCallback(() => {
    setFirstUse(false);
    try {
      localStorage.setItem(FIRST_USE_KEY, 'dismissed');
    } catch {
      // storage unavailable: hidden for this page only
    }
  }, []);
  const rows = useMemo(() => buildRows(service, state), [service, state]);
  const sum = summarize(rows, state);
  const finished = service.finished();
  const replaying = !!state.replay;
  const closeMenu = useCallback(() => setMenu(null), []);
  const ready = state.phase === 'ready';
  const activePreset = presetOf(state.voi);
  const noClass = !!state.voi && classifyWindow(state.voi.wc, state.voi.ww) === null;
  const params = state.coverage?.params;
  const custom = isCustomRules(params);
  const sample = state.origin.sample;
  const sheet = state.finishOpen && !replaying;

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

  // the session's events on the server (all tabs, earlier page loads, sample events), not
  // only this page's; this page's count until the first reply
  const info = state.coverage?.session_info;
  const nEvents = typeof info?.events === 'number' ? info.events : state.eventsSent;
  const eventsTitle =
    `Display and reader events recorded in this reading: ${nEvents}` +
    (info?.sample_events ? `, ${info.sample_events} of them from the sample` : '') +
    `. Sent from this page: ${state.eventsSent}.`;

  const countsTitle =
    `Organs by measured status: ${sum.reviewed} reviewed (● displayed enough to count), ` +
    `${sum.partial} partial (◐), ${sum.skipped} skipped (○)`;

  return (
    <div className="cm-root" data-testid="covmap-panel" data-phase={state.phase}>
      {state.otherTab && state.sessionId && (
        <div className="cm-other-tab" data-testid="covmap-other-tab" role="status">
          <span className="cm-grow">This reading is open in another tab — not recording here</span>
          <button
            className="cm-btn cm-btn--primary"
            data-testid="covmap-record-here"
            title="Record this reading in this tab; the other tab stops recording"
            onClick={() => service.recordHere()}
          >
            Record here
          </button>
        </div>
      )}
      <div className="cm-summary" data-testid="covmap-summary" data-finished={finished?.confirmed ? '1' : '0'}>
        <span className="cm-counts" title={countsTitle} aria-label={countsTitle}>
          <span className="cm-count">
            <i className="cm-dot cm-dot--reviewed" />
            {sum.reviewed}
          </span>
          <span className="cm-count">
            <i className="cm-dot cm-dot--partial" />
            {sum.partial}
          </span>
          <span className="cm-count">
            <i className="cm-dot cm-dot--skipped" />
            {sum.skipped}
          </span>
        </span>
        <span className="cm-muted">·</span>
        <span title="Organs not displayed enough to count and not marked done or N/A" data-testid="covmap-remaining">
          <b>{sum.remaining}</b> left
        </span>
        <span
          className="cm-muted"
          title="Share of all organ voxels displayed under a qualifying window long enough to count"
        >
          · {sum.voxelPct === null ? '–' : `${sum.voxelPct}%`} of voxels
        </span>
        <span className="cm-grow" />
        <button
          className="cm-btn"
          data-testid="covmap-session-menu"
          data-covmap-menu-toggle=""
          aria-expanded={!!menu}
          onClick={() => setMenu(m => (m ? null : 'main'))}
        >
          Session ▾
        </button>
      </div>
      <div className="cm-sub">
        {finished?.confirmed && !replaying && (
          <span className="cm-chip cm-chip--done" data-testid="covmap-finished" title="Reader confirmed finish">
            ✓ Finished
          </span>
        )}
        <span
          className="cm-ellipsis cm-sub-label"
          data-testid="covmap-session-label"
          title={state.sessionLabel || undefined}
        >
          {replaying ? 'Replay (read-only, recording paused): ' : ''}
          {state.sessionLabel || 'No session yet'}
        </span>
        {state.origin.resumed && !replaying && !state.otherTab && (
          <span className="cm-chip" data-testid="covmap-resumed" title="Continued after the page was reloaded">
            resumed
            <button
              className="cm-link"
              data-testid="covmap-new-reading-quick"
              data-covmap-menu-toggle=""
              onClick={() => setMenu('confirm-new')}
            >
              New reading
            </button>
          </span>
        )}
        {custom && (
          <button
            className="cm-chip cm-chip--rules"
            data-testid="covmap-custom-rules"
            data-covmap-menu-toggle=""
            disabled={replaying}
            title={`Custom research rules: min dwell ${params!.min_dwell_ms} ms, reviewed at ${Math.round(
              params!.reviewed_threshold * 100
            )}%, skipped below ${Math.round(params!.skipped_threshold * 100)}%. ${
              replaying ? 'Close replay to change them.' : 'Click to change or restore defaults.'
            }`}
            onClick={() => setMenu('rules')}
          >
            custom rules
          </button>
        )}
        {(sum.attested > 0 || sum.na > 0 || sum.flagged > 0) && (
          <span>
            {sum.attested ? `${sum.attested} done` : ''}
            {sum.attested && (sum.na || sum.flagged) ? ' · ' : ''}
            {sum.na ? `${sum.na} N/A` : ''}
            {sum.na && sum.flagged ? ' · ' : ''}
            {sum.flagged ? `${sum.flagged} flagged` : ''}
          </span>
        )}
        <span data-testid="covmap-events" title={eventsTitle}>
          {nEvents} {nEvents === 1 ? 'event' : 'events'}
        </span>
      </div>
      {sample && (
        <div className="cm-sub" data-testid="covmap-origin">
          <span className="cm-chip cm-chip--sample cm-ellipsis" title={`Sample: ${sample.label}`}>
            sample events: {sourceText(sample.source)}
            {typeof sample.events === 'number' ? ` · ${sample.events}` : ''}
          </span>
        </div>
      )}
      {firstUse && state.caseId && (
        <div className="cm-first-use" data-testid="covmap-first-use" role="note">
          <span className="cm-grow">
            Scroll the CT; organs turn green once shown in their window · <span className="cm-kbd">N</span> next gap
          </span>
          <button
            className="cm-first-use-x"
            data-testid="covmap-first-use-dismiss"
            title="Hide this hint (remembered in this browser)"
            aria-label="Hide the first-use hint"
            onClick={dismissFirstUse}
          >
            ×
          </button>
        </div>
      )}
      {menu && <SessionMenu key={menu} service={service} state={state} initial={menu} onClose={closeMenu} />}

      {message && <div className="cm-msg">{message}</div>}
      {sheet && <FinishSheet service={service} rows={rows} />}
      {state.caseId && (
        <OrganMapView
          hidden={sheet}
          service={service}
          caseId={state.caseId}
          rows={rows}
          highlight={hot ?? state.selected}
          onHover={setHot}
          replaying={replaying}
        />
      )}
      {state.caseId &&
        !sheet &&
        (state.replay ? (
          <ReplayBar service={service} replay={state.replay} />
        ) : (
          <div className="cm-actions">
            <button
              className="cm-btn cm-btn--primary"
              data-testid="covmap-next-gap"
              disabled={!ready}
              title="Jump to the next organ not displayed enough, on a slice it still has unseen, in its window (N)"
              onClick={() => service.nextGap()}
            >
              Next gap <span className="cm-kbd">N</span>
            </button>
            {(['soft', 'lung', 'bone'] as const).map(id => (
              <button
                key={id}
                className={`cm-btn${activePreset?.id === id ? ' cm-btn--on' : ''}`}
                data-testid={`covmap-window-${id}`}
                aria-pressed={activePreset?.id === id}
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
      {state.caseId && !replaying && !sheet && noClass && (
        <div
          className="cm-window-hint"
          data-testid="covmap-window-hint"
          role="status"
          title={`W ${Math.round(state.voi!.ww)} / L ${Math.round(state.voi!.wc)} qualifies for no organ's window class`}
        >
          This window counts for no organ — press{' '}
          <span className="cm-kbd">1</span> soft, <span className="cm-kbd">2</span> lung,{' '}
          <span className="cm-kbd">3</span> bone
        </div>
      )}
      {state.caseId && !sheet && (
        <OrganList
          service={service}
          rows={rows}
          selected={state.selected}
          hot={hot}
          replaying={replaying}
          gaps={state.gaps}
          sessionId={state.sessionId}
          onHover={setHot}
        />
      )}
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
