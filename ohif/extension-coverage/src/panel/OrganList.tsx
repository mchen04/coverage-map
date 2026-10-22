import React, { useEffect, useRef, useState } from 'react';
import { CLASS_BADGE } from '../presets';
import type CoverageService from '../CoverageService';
import type { Gap, WindowClass } from '../types';
import { pct, rangesText, STATUS_WORD, unseenHint, type Row } from './model';

const GROUP_TITLE: Record<WindowClass, string> = {
  soft: 'Soft-tissue window',
  lung: 'Lung window',
  bone: 'Bone window',
};

export function StatusDot({ status }: { status: Row['status'] }) {
  return <i className={`cm-dot cm-dot--${status}`} title={STATUS_WORD[status]} />;
}

export function WindowBadge({ windows }: { windows: WindowClass[] }) {
  return (
    <span className="cm-badge" title={windows.map(w => CLASS_BADGE[w].title).join('; ')}>
      {windows.map(w => (
        <b key={w} style={{ background: CLASS_BADGE[w].color }}>
          {CLASS_BADGE[w].letter}
        </b>
      ))}
    </span>
  );
}

interface Props {
  service: CoverageService;
  rows: Row[];
  selected: string | null;
  hot: string | null;
  replaying: boolean;
  gaps: Record<string, Gap>;
  sessionId: string | null;
  onHover: (key: string | null) => void;
}

export default function OrganList({ service, rows, selected, hot, replaying, gaps, sessionId, onHover }: Props) {
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!selected || !listRef.current) {
      return;
    }
    const el = listRef.current.querySelector(`[data-key="${selected}"]`);
    el?.scrollIntoView({ block: 'nearest' });
  }, [selected]);

  // a new reading starts at the top of the list
  useEffect(() => {
    if (listRef.current) {
      listRef.current.scrollTop = 0;
    }
  }, [sessionId]);

  const groups: Array<[WindowClass, Row[]]> = (['soft', 'lung', 'bone'] as WindowClass[])
    .map(cls => [cls, rows.filter(r => (r.organ.windows?.[0] ?? 'soft') === cls)] as [WindowClass, Row[]])
    .filter(([, list]) => list.length > 0);

  return (
    <div
      className="cm-list"
      ref={listRef}
      data-testid="covmap-organ-list"
      onMouseLeave={() => onHover(null)}
    >
      {groups.map(([cls, list]) => (
        <div key={cls}>
          <div className="cm-group">
            <span>{GROUP_TITLE[cls]}</span>
            <span>
              {list.filter(r => r.status === 'reviewed').length}/{list.length} reviewed
            </span>
          </div>
          {list.map(row => (
            <OrganRow
              key={row.organ.key}
              service={service}
              row={row}
              selected={selected === row.organ.key}
              hot={hot === row.organ.key}
              replaying={replaying}
              gap={gaps[row.organ.key] ?? null}
              onHover={onHover}
            />
          ))}
        </div>
      ))}
    </div>
  );
}

function OrganRow({
  service,
  row,
  selected,
  hot,
  replaying,
  gap,
  onHover,
}: {
  service: CoverageService;
  row: Row;
  selected: boolean;
  hot: boolean;
  replaying: boolean;
  gap: Gap | null;
  onHover: (key: string | null) => void;
}) {
  const { organ, status, layer } = row;
  const key = organ.key;
  const hint = unseenHint(row);
  const [note, setNote] = useState(layer.note ?? '');
  const noteRef = useRef<HTMLInputElement>(null);
  const wasFlagged = useRef(layer.flagged);
  useEffect(() => setNote(layer.note ?? ''), [layer.note]);
  // just flagged (G or the flag button): bring the note field into view and focus it
  useEffect(() => {
    const now = layer.flagged;
    if (now && !wasFlagged.current && selected && !replaying) {
      requestAnimationFrame(() => {
        noteRef.current?.scrollIntoView({ block: 'nearest' });
        noteRef.current?.focus();
      });
    }
    wasFlagged.current = now;
  }, [layer.flagged, selected, replaying]);

  // the detail grows when the gap target arrives: keep a focused note field in view
  useEffect(() => {
    const el = noteRef.current;
    if (el && document.activeElement === el) {
      el.scrollIntoView({ block: 'nearest' });
    }
  }, [gap]);

  // image numbers as the viewer shows them (n of n/N), not case slice indices
  const extent = selected && organ.k_range ? service.viewerRanges([organ.k_range]) : null;
  const todo = selected && gap && !replaying ? service.viewerRanges(gap.ranges) : null;

  const cls = [
    'cm-organ',
    selected && 'cm-organ--selected',
    hot && 'cm-organ--hot',
    layer.reader === 'done' && 'cm-organ--done',
    layer.reader === 'na' && 'cm-organ--na',
  ]
    .filter(Boolean)
    .join(' ');
  const stop = (fn: () => void) => (e: React.MouseEvent) => {
    e.stopPropagation();
    fn();
  };
  const barColor = `var(--cm-${status})`;
  const title =
    `${organ.name}: ${pct(row.coverage)} of voxels displayed under a qualifying window (${STATUS_WORD[status]})` +
    (hint ? `. Unseen: ${hint.full}` : '');

  return (
    <div
      className={cls}
      data-key={key}
      data-status={status}
      data-reader={layer.reader ?? ''}
      data-flagged={layer.flagged ? '1' : '0'}
      data-testid={`covmap-organ-${key}`}
      onMouseEnter={() => onHover(key)}
    >
      <div
        className="cm-organ-line"
        title={title}
        onClick={() => (replaying ? service.select(key) : service.jumpToOrgan(key))}
      >
        <StatusDot status={status} />
        <span className="cm-name">{organ.name}</span>
        {hint && <span className="cm-hint">{hint.short}</span>}
        <WindowBadge windows={organ.windows ?? ['soft']} />
        <span className="cm-bar">
          <i style={{ width: `${Math.round((row.coverage ?? 0) * 100)}%`, background: barColor }} />
        </span>
        <span className="cm-pct">{pct(row.coverage)}</span>
        <span className="cm-act">
          <button
            data-testid={`covmap-organ-${key}-done`}
            className={layer.reader === 'done' ? 'cm-on-done' : ''}
            title="Mark done (D): reader attests review. Kept separate from the measured status."
            aria-label={`Mark ${organ.name} done`}
            aria-pressed={layer.reader === 'done'}
            disabled={replaying}
            onClick={stop(() => service.toggleDone(key))}
          >
            ✓
          </button>
          <button
            data-testid={`covmap-organ-${key}-na`}
            className={layer.reader === 'na' ? 'cm-on-na' : ''}
            title="Not applicable (A): absent or out of scope; excluded from remaining"
            aria-label={`Mark ${organ.name} not applicable`}
            aria-pressed={layer.reader === 'na'}
            disabled={replaying}
            onClick={stop(() => service.toggleNA(key))}
          >
            N/A
          </button>
          <button
            data-testid={`covmap-organ-${key}-flag`}
            className={layer.flagged ? 'cm-on-flag' : ''}
            title="Flag suspicious segmentation (G)"
            aria-label={`Flag the ${organ.name} segmentation`}
            aria-pressed={layer.flagged}
            disabled={replaying}
            onClick={stop(() => {
              service.select(key);
              service.toggleFlag(key);
            })}
          >
            ⚑
          </button>
        </span>
      </div>
      {selected && (
        <div className="cm-detail" data-testid={`covmap-organ-${key}-detail`}>
          {layer.flagged && !replaying && (
            <input
              ref={noteRef}
              data-testid={`covmap-organ-${key}-note`}
              aria-label={`Flag note for ${organ.name}`}
              placeholder="Flag note (optional): Enter saves, Esc leaves"
              value={note}
              maxLength={200}
              onClick={e => e.stopPropagation()}
              onChange={e => setNote(e.target.value)}
              onKeyDown={e => {
                e.stopPropagation();
                if (e.key === 'Enter') {
                  service.toggleFlag(key, note.trim());
                  (e.target as HTMLInputElement).blur();
                } else if (e.key === 'Escape') {
                  setNote(layer.note ?? '');
                  (e.target as HTMLInputElement).blur();
                }
              }}
            />
          )}
          {hint
            ? `Unseen: ${hint.full}.`
            : status === 'reviewed'
              ? 'Displayed enough to count as reviewed.'
              : replaying
                ? `${pct(row.coverage)} displayed at this point of the replay.`
                : 'No display data yet.'}{' '}
          {layer.reader === 'done' && status !== 'reviewed' && 'Marked done by reader (attestation, not measured). '}
          {layer.reader === 'na' && 'Marked not applicable. '}
          {extent && extent.ranges.length > 0 && (
            <span className="cm-muted" data-testid={`covmap-organ-${key}-extent`}>
              On images {rangesText(extent.ranges, 2)} of {extent.n}.{' '}
            </span>
          )}
          {todo && gap && (
            <span data-testid={`covmap-organ-${key}-gap`} className="cm-todo">
              {gap.unseen_voxels > 0 && todo.ranges.length > 0
                ? `Still to read: images ${rangesText(todo.ranges)}. `
                : gap.unseen_voxels > 0
                  ? 'Unseen parts are in view; window or dwell is missing. '
                  : 'Nothing left unseen. '}
            </span>
          )}
          <span className="cm-muted">O: outline on image.</span>
        </div>
      )}
    </div>
  );
}
