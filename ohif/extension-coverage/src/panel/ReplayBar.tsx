import React from 'react';
import type CoverageService from '../CoverageService';
import type { ReplayState } from '../types';

function fmt(ms: number): string {
  const s = Math.max(0, ms) / 1000;
  return s < 60 ? `${s.toFixed(1)} s` : `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
}

export default function ReplayBar({ service, replay }: { service: CoverageService; replay: ReplayState }) {
  const frame = replay.frames[replay.index];
  const t0 = replay.frames[0]?.t_ms ?? 0;
  const at = fmt((frame?.t_ms ?? 0) - t0);
  const total = fmt((replay.frames[replay.frames.length - 1]?.t_ms ?? 0) - t0);
  return (
    <div className="cm-replay" data-testid="covmap-replay-bar" role="group" aria-label="Session replay">
      <button
        className="cm-btn"
        data-testid="covmap-replay-play"
        title={replay.playing ? 'Pause replay' : 'Play replay'}
        aria-label={replay.playing ? 'Pause replay' : 'Play replay'}
        onClick={() => (replay.playing ? service.pauseReplay() : service.playReplay())}
      >
        {replay.playing ? '❚❚' : '▶'}
      </button>
      <input
        type="range"
        min={0}
        max={replay.frames.length - 1}
        value={replay.index}
        data-testid="covmap-replay-slider"
        aria-label="Replay position"
        aria-valuetext={`${at} of ${total}`}
        onChange={e => {
          service.pauseReplay();
          service.setReplayIndex(Number(e.target.value));
        }}
        onKeyDown={e => e.stopPropagation()}
      />
      <span className="cm-muted" style={{ fontVariantNumeric: 'tabular-nums' }} aria-hidden="true">
        {at}
      </span>
      <button
        className="cm-btn"
        data-testid="covmap-replay-close"
        title="Close replay and return to the live reading (recording resumes)"
        aria-label="Close replay"
        onClick={() => service.stopReplay()}
      >
        ✕
      </button>
    </div>
  );
}
