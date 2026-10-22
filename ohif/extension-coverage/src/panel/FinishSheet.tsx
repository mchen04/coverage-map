import React, { useEffect } from 'react';
import type CoverageService from '../CoverageService';
import { StatusDot, WindowBadge } from './OrganList';
import { pct, STATUS_WORD, unseenHint, type Row } from './model';

interface Props {
  service: CoverageService;
  rows: Row[];
}

/**
 * Before the reader finishes: organs not displayed enough to count and not marked done or
 * N/A, then the reader's own decisions (done below the threshold, N/A, flagged). Wording is
 * about what was displayed, never a clinical review.
 */
export default function FinishSheet({ service, rows }: Props) {
  const remaining = new Set(service.remaining());
  const list = rows.filter(r => remaining.has(r.organ.key));
  const doneBelow = rows.filter(r => r.layer.reader === 'done' && r.status !== 'reviewed');
  const na = rows.filter(r => r.layer.reader === 'na');
  const flagged = rows.filter(r => r.layer.flagged);
  const decisions = doneBelow.length + na.length + flagged.length;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // one Escape closes one thing: an open Session menu first, then this sheet
      if (e.key === 'Escape' && !document.querySelector('[data-testid="covmap-session-popover"]')) {
        e.stopPropagation();
        service.finish(false);
      }
    };
    document.addEventListener('keydown', onKey, true);
    return () => document.removeEventListener('keydown', onKey, true);
  }, [service]);

  return (
    <div className="cm-sheet" data-testid="covmap-finish-dialog" role="dialog" aria-label="Finish reading">
      <div className="cm-row-flex">
        <b className="cm-grow">Finish reading</b>
        <span className="cm-muted">{list.length} not displayed enough</span>
      </div>
      <div className="cm-muted" style={{ fontSize: 11 }}>
        {list.length
          ? 'Not displayed enough to count (slice, region, window, dwell) and not marked done or N/A. Click one to go there.'
          : 'Every organ was displayed enough to count, or is marked done or N/A.'}
      </div>
      <div className="cm-sheet-list" data-testid="covmap-finish-list">
        {list.map(row => {
          const hint = unseenHint(row);
          return (
            <div key={row.organ.key} className="cm-organ" data-testid={`covmap-finish-${row.organ.key}`}>
              <div
                className="cm-organ-line"
                title={hint ? `Unseen: ${hint.full}` : STATUS_WORD[row.status]}
                onClick={() => service.jumpToOrgan(row.organ.key)}
              >
                <StatusDot status={row.status} />
                <span className="cm-label">
                  <span className="cm-name">{row.organ.name}</span>
                  {hint && <span className="cm-hint">{hint.short}</span>}
                </span>
                <WindowBadge windows={row.organ.windows ?? ['soft']} />
                <span className="cm-pct">{pct(row.coverage)}</span>
              </div>
            </div>
          );
        })}
      </div>
      <div className="cm-decisions" data-testid="covmap-finish-decisions">
        <div className="cm-pop-title">Your decisions ({decisions})</div>
        {decisions === 0 && <div className="cm-muted">None: nothing marked done, N/A or flagged.</div>}
        {doneBelow.map(r => (
          <div key={`d-${r.organ.key}`} data-testid={`covmap-decision-done-${r.organ.key}`}>
            <span className="cm-dec cm-dec--done">done</span> {r.organ.name}{' '}
            <span className="cm-muted">marked done at {pct(r.coverage)} displayed</span>
          </div>
        ))}
        {na.length > 0 && (
          <div data-testid="covmap-decision-na">
            <span className="cm-dec cm-dec--na">N/A</span> {na.map(r => r.organ.name).join(', ')}
          </div>
        )}
        {flagged.map(r => (
          <div key={`f-${r.organ.key}`} data-testid={`covmap-decision-flag-${r.organ.key}`}>
            <span className="cm-dec cm-dec--flag">flag</span> {r.organ.name}
            {r.layer.note ? <span className="cm-muted">: “{r.layer.note}”</span> : null}
          </div>
        ))}
      </div>
      <div className="cm-row-flex" style={{ justifyContent: 'flex-end' }}>
        <button
          className="cm-btn"
          data-testid="covmap-go-back"
          title="Back to reading (Esc)"
          onClick={() => service.finish(false)}
        >
          Go back
        </button>
        <button
          className="cm-btn cm-btn--primary"
          data-testid="covmap-confirm-finish"
          onClick={() => service.finish(true)}
        >
          Confirm finish{list.length ? ` (${list.length} not displayed enough)` : ''}
        </button>
      </div>
    </div>
  );
}
