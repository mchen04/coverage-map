export type InBrowserConfig = {
  worker: string;
  dataRoot: string;
  pythonZip: string;
  pyodideUrl: string;
  demoCase?: string;
};

type Reply = { id: number; status?: number; headers?: Record<string, string>; body?: string; error?: string };

const STATIC_CASE_FILES: Record<string, string> = {
  organs: 'seg/organs.json',
  volume: 'volume.json',
  'meshes.json': 'seg/meshes.json',
  'meshes.bin': 'seg/meshes.bin',
};

export class InBrowserServer {
  private worker: Worker;
  private nextId = 0;
  private pending = new Map<number, (reply: Reply) => void>();
  public readonly ready: Promise<void>;

  constructor(private readonly config: InBrowserConfig) {
    this.worker = new Worker(config.worker);
    this.ready = new Promise((resolve, reject) => {
      this.worker.onmessage = ({ data }) => {
        if (data.type === 'ready') {
          resolve();
        } else if (data.type === 'failed') {
          reject(new Error(data.error));
        } else {
          this.pending.get(data.id)?.(data);
          this.pending.delete(data.id);
        }
      };
    });
    this.worker.postMessage({
      type: 'boot',
      config: {
        pyodideUrl: config.pyodideUrl,
        dataRoot: new URL(config.dataRoot, location.href).href.replace(/\/$/, ''),
        pythonZip: new URL(config.pythonZip, location.href).href,
        demoCase: config.demoCase ?? 'pancreas-0051',
      },
    });
  }

  caseFilesBase(caseId: string): string {
    return `${this.config.dataRoot.replace(/\/$/, '')}/cases/${encodeURIComponent(caseId)}`;
  }

  meshBase(caseId: string): string {
    return `${this.caseFilesBase(caseId)}/seg`;
  }

  viewerUrl(caseId: string, studyUid: string): string {
    const manifest = `${this.config.dataRoot.replace(/\/$/, '')}/dicom/${encodeURIComponent(caseId)}.json`;
    return `/coverage?url=${encodeURIComponent(manifest)}&StudyInstanceUIDs=${encodeURIComponent(studyUid)}`;
  }

  async fetch(url: string, init?: RequestInit): Promise<Response> {
    const u = new URL(url, location.href);
    const file = /^\/api\/cases\/([^/]+)\/(organs|volume|meshes\.json|meshes\.bin)$/.exec(u.pathname);
    if (file && (init?.method ?? 'GET') === 'GET') {
      return fetch(`${this.caseFilesBase(decodeURIComponent(file[1]))}/${STATIC_CASE_FILES[file[2]]}`);
    }
    await this.ready;
    const id = this.nextId++;
    const reply = await new Promise<Reply>(resolve => {
      this.pending.set(id, resolve);
      this.worker.postMessage({
        id,
        method: init?.method ?? 'GET',
        path: u.pathname + u.search,
        body: typeof init?.body === 'string' ? init.body : undefined,
      });
    });
    if (reply.error !== undefined) {
      throw new Error(reply.error);
    }
    return new Response(reply.body, { status: reply.status, headers: reply.headers });
  }
}
