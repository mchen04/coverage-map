import { fetchMeshes } from '@covmap/organ-map';
import type { CaseInfo, Coverage, Gap, Organ, Rules, Sample, SessionInfo, TimelineFrame, Volume } from './types';

/** A failed API request: HTTP status and the parsed JSON error body (null if none). */
export type ApiError = Error & { status?: number; body?: Record<string, unknown> | null };

/** Thin client for the covmap HTTP API (docs/SPEC.md "HTTP API"). Same origin. */
export class CovmapApi {
  constructor(public readonly root: string = '/api') {
    this.root = root.replace(/\/$/, '');
  }

  private async json<T>(path: string, init?: RequestInit): Promise<T> {
    const res = await fetch(`${this.root}${path}`, init);
    if (!res.ok) {
      const err = new Error(`${init?.method ?? 'GET'} ${path} -> ${res.status}`) as ApiError;
      err.status = res.status;
      try {
        err.body = await res.json(); // the API's JSON error ({error, ...}; a 409 adds last_seq, last_t_ms)
      } catch {
        err.body = null;
      }
      throw err;
    }
    return res.json() as Promise<T>;
  }

  private post<T>(path: string, body: unknown, extra: RequestInit = {}): Promise<T> {
    return this.json<T>(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      ...extra,
    });
  }

  async lookup(params: { series?: string; study?: string }): Promise<string | null> {
    const q = params.series ? `series=${encodeURIComponent(params.series)}` : `study=${encodeURIComponent(params.study ?? '')}`;
    try {
      return (await this.json<{ case: string }>(`/lookup?${q}`)).case ?? null;
    } catch (e) {
      if ((e as { status?: number }).status === 404) {
        return null;
      }
      throw e;
    }
  }

  cases() {
    return this.json<CaseInfo[]>('/cases');
  }

  /** Session info (last_seq / last_t_ms to continue a session after a reload). */
  session(id: string) {
    return this.json<SessionInfo>(`/sessions/${encodeURIComponent(id)}`);
  }

  /** The session's coverage from the events already accepted (no new events). */
  coverage(id: string) {
    return this.json<Coverage>(`/sessions/${encodeURIComponent(id)}/coverage`);
  }

  /** The session's accepted event log (JSONL), parsed; unparsable lines are skipped. */
  async eventLog(id: string): Promise<Array<Record<string, any>>> {
    const res = await fetch(`${this.root}/sessions/${encodeURIComponent(id)}/events.jsonl`);
    if (!res.ok) {
      throw Object.assign(new Error(`GET events.jsonl -> ${res.status}`), { status: res.status });
    }
    const out: Array<Record<string, any>> = [];
    for (const line of (await res.text()).split('\n')) {
      if (line.trim()) {
        try {
          out.push(JSON.parse(line));
        } catch {
          // a partly written last line
        }
      }
    }
    return out;
  }

  /** Where the organ still has unseen voxels; nowMs credits the open display state. */
  gap(session: string, organ: string, nowMs?: number, signal?: AbortSignal) {
    const now = nowMs === undefined ? '' : `&now_ms=${nowMs}`;
    return this.json<Gap>(
      `/sessions/${encodeURIComponent(session)}/gap?organ=${encodeURIComponent(organ)}${now}`,
      signal ? { signal } : undefined
    );
  }

  createSession(caseId: string, label?: string, fromSample?: string) {
    const body: Record<string, string> = { case: caseId };
    if (label) {
      body.label = label;
    }
    if (fromSample) {
      body.from_sample = fromSample;
    }
    return this.post<{ session: string; case: string }>('/sessions', body);
  }

  postEvents(session: string, events: unknown[], nowMs: number, keepalive = false) {
    return this.post<Coverage>(
      `/sessions/${encodeURIComponent(session)}/events`,
      { events, now_ms: nowMs },
      keepalive ? { keepalive: true } : {}
    );
  }

  beacon(session: string, events: unknown[], nowMs: number): boolean {
    const blob = new Blob([JSON.stringify({ events, now_ms: nowMs })], { type: 'application/json' });
    return navigator.sendBeacon?.(`${this.root}/sessions/${encodeURIComponent(session)}/events`, blob) ?? false;
  }

  organs(caseId: string) {
    return this.json<{ organs: Organ[] }>(`/cases/${encodeURIComponent(caseId)}/organs`);
  }

  volume(caseId: string) {
    return this.json<Volume>(`/cases/${encodeURIComponent(caseId)}/volume`);
  }

  meshBase(caseId: string) {
    return `${this.root}/cases/${encodeURIComponent(caseId)}`;
  }

  meshes(caseId: string) {
    return fetchMeshes(this.meshBase(caseId));
  }

  samples(caseId: string) {
    return this.json<Sample[]>(`/cases/${encodeURIComponent(caseId)}/samples`);
  }

  outline(caseId: string, organ: string, k: number) {
    return this.json<{ contours: [number, number][][] }>(
      `/cases/${encodeURIComponent(caseId)}/outline?organ=${encodeURIComponent(organ)}&k=${k}`
    );
  }

  rules(session: string, rules: Partial<Rules>) {
    return this.post<Coverage>(`/sessions/${encodeURIComponent(session)}/rules`, rules);
  }

  timeline(session: string, frames = 40) {
    return this.json<{ frames: TimelineFrame[] }>(
      `/sessions/${encodeURIComponent(session)}/timeline?frames=${frames}`
    );
  }

  exportUrl(session: string, format: 'jsonl' | 'json' | 'csv') {
    return `${this.root}/sessions/${encodeURIComponent(session)}/export?format=${format}`;
  }
}
