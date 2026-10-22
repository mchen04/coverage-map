import fs from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';

const dcmjs = createRequire(path.join(process.cwd(), 'package.json'))('dcmjs');
const [dir, urlPrefix, out] = process.argv.slice(2);
const STUDY_KEYS = ['StudyInstanceUID', 'StudyDescription', 'StudyDate', 'StudyTime', 'PatientName',
  'PatientID', 'AccessionNumber', 'PatientAge', 'PatientSex', 'PatientWeight'];
const SERIES_KEYS = ['SeriesInstanceUID', 'SeriesDescription', 'SeriesNumber', 'SeriesTime', 'Modality',
  'SliceThickness'];
const pick = (meta, keys) => Object.fromEntries(keys.map(k => [k, meta[k]]));

const names = (await fs.readdir(dir)).filter(n => n.endsWith('.dcm')).sort();
const studies = [];
for (const name of names) {
  const buf = await fs.readFile(path.join(dir, name));
  const dict = dcmjs.data.DicomMessage.readFile(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength));
  const meta = dcmjs.data.DicomMetaDictionary.naturalizeDataset(dict.dict);
  delete meta.PixelData;
  delete meta._vrMap;
  let study = studies.find(s => s.StudyInstanceUID === meta.StudyInstanceUID);
  if (!study) {
    study = { ...pick(meta, STUDY_KEYS), PatientID: meta.PatientID || 'anonymous', series: [] };
    studies.push(study);
  }
  let series = study.series.find(s => s.SeriesInstanceUID === meta.SeriesInstanceUID);
  if (!series) {
    series = { ...pick(meta, SERIES_KEYS), instances: [] };
    study.series.push(series);
  }
  series.instances.push({ metadata: meta, url: `dicomweb:${urlPrefix.replace(/\/$/, '')}/${name}` });
}
for (const study of studies) {
  study.NumInstances = study.series.reduce((n, s) => n + s.instances.length, 0);
  study.Modalities = [...new Set(study.series.map(s => s.Modality))].join('/');
}
await fs.writeFile(out, JSON.stringify({ studies }));
console.log(JSON.stringify({ out, studies: studies.length, instances: studies.reduce((n, s) => n + s.NumInstances, 0) }));
