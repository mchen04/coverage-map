let ready = null;
let persist = Promise.resolve();

async function bytes(url) {
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`GET ${url} -> ${res.status}`);
  }
  if (!url.endsWith('.gz')) {
    return new Uint8Array(await res.arrayBuffer());
  }
  const plain = res.body.pipeThrough(new DecompressionStream('gzip'));
  return new Uint8Array(await new Response(plain).arrayBuffer());
}

function syncfs(FS, populate) {
  return new Promise((resolve, reject) => FS.syncfs(populate, err => (err ? reject(err) : resolve())));
}

async function boot({ pyodideUrl, dataRoot, pythonZip, demoCase }) {
  importScripts(`${pyodideUrl}pyodide.js`);
  const pyodide = await loadPyodide({ indexURL: pyodideUrl });
  await pyodide.loadPackage(['numpy', 'aiohttp'], { messageCallback: () => {} });
  const FS = pyodide.FS;

  pyodide.unpackArchive(await bytes(pythonZip), 'zip', { extractDir: '/app' });
  const { files } = await (await fetch(`${dataRoot}/files.json`)).json();
  await Promise.all(
    files.map(async rel => {
      const target = `/data/${rel.replace(/\.gz$/, '')}`;
      FS.mkdirTree(target.slice(0, target.lastIndexOf('/')));
      FS.writeFile(target, await bytes(`${dataRoot}/${rel}`));
    })
  );

  FS.mkdirTree('/data/sessions');
  FS.mount(FS.filesystems.IDBFS, {}, '/data/sessions');
  await syncfs(FS, true);

  pyodide.globals.set('demo_case', demoCase);
  await pyodide.runPythonAsync(`
import json
import sys
from pathlib import Path

sys.path.insert(0, "/app")
from covmap.browser import BrowserServer
from covmap.server import Config

server = BrowserServer(Config(data_dir=Path("/data"), ohif_dist=Path("/nonexistent"), demo_case=demo_case))
await server.start()


async def dispatch(method, path, body):
    status, headers, data = await server.handle(method, path, body.encode() if body else None)
    return status, json.dumps(headers), data.decode()
`);
  return { pyodide, FS, dispatch: pyodide.globals.get('dispatch') };
}

self.onmessage = async ({ data }) => {
  if (data.type === 'boot') {
    ready = boot(data.config);
    ready.then(
      () => self.postMessage({ type: 'ready' }),
      e => self.postMessage({ type: 'failed', error: String(e?.message ?? e) })
    );
    return;
  }
  const { id, method, path, body } = data;
  try {
    const { FS, dispatch } = await ready;
    const result = await dispatch(method, path, body ?? '');
    const [status, headers, text] = result.toJs();
    result.destroy();
    self.postMessage({ id, status, headers: JSON.parse(headers), body: text });
    if (method !== 'GET') {
      persist = persist.then(() => syncfs(FS, false)).catch(() => {});
    }
  } catch (e) {
    self.postMessage({ id, error: String(e?.message ?? e) });
  }
};
