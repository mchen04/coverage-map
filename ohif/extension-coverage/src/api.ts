import { fetchMeshes } from '@covmap/organ-map';
import type { Coverage, Organ, Rules, Sample, TimelineFrame, Volume } from './types';

/** Thin client for the covmap HTTP API (docs/SPEC.md "HTTP API"). Same origin. */
export class CovmapApi {
  constructor(public readonly root: string = '/api') {
    this.root = root.replace(/\/$/, '');
  }

  private async json<T>(path: string, init?: RequestInit): Promise<T> {
    const res = await fetch(`${this.root}${path}`, init);
    if (!res.ok) {
      const err = new Error(`${init?.method ?? 'GET'} ${path} -> ${res.status}`) as Error & {
        status?: number;
      };
      err.status = res.status;
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
