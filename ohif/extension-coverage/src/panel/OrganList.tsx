import React, { useEffect, useRef, useState } from 'react';
import { CLASS_BADGE } from '../presets';
import type CoverageService from '../CoverageService';
import type { WindowClass } from '../types';
import { pct, STATUS_WORD, unseenHint, type Row } from './model';

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
  onHover: (key: string | null) => void;
}

export default function OrganList({ service, rows, selected, hot, replaying, onHover }: Props) {
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!selected || !listRef.current) {
      return;
    }
    const el = listRef.current.querySelector(`[data-key="${selected}"]`);
    el?.scrollIntoView({ block: 'nearest' });
  }, [selected]);

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
  onHover,
}: {
  service: CoverageService;
  row: Row;
  selected: boolean;
  hot: boolean;
  replaying: boolean;
  onHover: (key: string | null) => void;
}) {
  const { organ, status, layer } = row;
  const key = organ.key;
  const hint = unseenHint(row);
  const [note, setNote] = useState(layer.note ?? '');
  useEffect(() => setNote(layer.note ?? ''), [layer.note]);

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
          {hint
            ? `Unseen: ${hint.full}.`
            : status === 'reviewed'
              ? 'Displayed enough to count as reviewed.'
              : replaying
                ? `${pct(row.coverage)} displayed at this point of the replay.`
                : 'No display data yet.'}{' '}
          {layer.reader === 'done' && status !== 'reviewed' && 'Marked done by reader (attestation, not measured). '}
          {layer.reader === 'na' && 'Marked not applicable. '}
          <span className="cm-muted">
            {organ.k_range ? `Slices ${organ.k_range[0] + 1}–${organ.k_range[1] + 1}. ` : ''}O: outline on image.
          </span>
          {layer.flagged && !replaying && (
            <input
              data-testid={`covmap-organ-${key}-note`}
              placeholder="Flag note (optional), Enter to save"
              value={note}
              maxLength={200}
              onClick={e => e.stopPropagation()}
              onChange={e => setNote(e.target.value)}
              onKeyDown={e => {
                e.stopPropagation();
                if (e.key === 'Enter') {
                  service.toggleFlag(key, note.trim());
                  (e.target as HTMLInputElement).blur();
                }
              }}
            />
          )}
        </div>
      )}
    </div>
  );
}
