import React from 'react';
import type CoverageService from '../CoverageService';
import { StatusDot, WindowBadge } from './OrganList';
import { pct, STATUS_WORD, unseenHint, type Row } from './model';

interface Props {
  service: CoverageService;
  rows: Row[];
}

/** Remaining organs (not reviewed, not marked done or N/A) before the reader finishes. */
export default function FinishSheet({ service, rows }: Props) {
  const remaining = new Set(service.remaining());
  const list = rows.filter(r => remaining.has(r.organ.key));
  return (
    <div className="cm-sheet" data-testid="covmap-finish-dialog" role="dialog" aria-label="Finish reading">
      <div className="cm-row-flex">
        <b className="cm-grow">Finish reading</b>
        <span className="cm-muted">{list.length} remaining</span>
      </div>
      <div className="cm-muted" style={{ fontSize: 11 }}>
        {list.length
          ? 'These organs were not displayed enough to count as reviewed and are not marked done or N/A. Click one to go there.'
          : 'Every organ is reviewed, marked done, or N/A.'}
      </div>
      <div className="cm-sheet-list" data-testid="covmap-finish-list">
        {list.map(row => {
          const hint = unseenHint(row);
          return (
            <div
              key={row.organ.key}
              className="cm-organ"
              data-testid={`covmap-finish-${row.organ.key}`}
            >
              <div
                className="cm-organ-line"
                title={hint ? `Unseen: ${hint.full}` : STATUS_WORD[row.status]}
                onClick={() => service.jumpToOrgan(row.organ.key)}
              >
                <StatusDot status={row.status} />
                <span className="cm-name">{row.organ.name}</span>
                {hint && <span className="cm-hint">{hint.short}</span>}
                <WindowBadge windows={row.organ.windows ?? ['soft']} />
                <span className="cm-pct">{pct(row.coverage)}</span>
              </div>
            </div>
          );
        })}
      </div>
      <div className="cm-row-flex" style={{ justifyContent: 'flex-end' }}>
        <button className="cm-btn" data-testid="covmap-go-back" onClick={() => service.finish(false)}>
          Go back
        </button>
        <button
          className="cm-btn cm-btn--primary"
          data-testid="covmap-confirm-finish"
          onClick={() => service.finish(true)}
        >
          Confirm finish{list.length ? ` (${list.length} not reviewed)` : ''}
        </button>
      </div>
    </div>
  );
}
