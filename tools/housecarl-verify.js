// Read-only independent xEdit gate. No save, cleanup, activation or registry writes.
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const [game, plugin, output] = process.argv.slice(2);
const base = process.env.HOUSECARL_XELIB_ROOT || path.resolve(__dirname, '..');
const {header} = require('./housecarl-preserve');

function recordLocations(filename) {
  const masters = header(filename).masters;
  const own = path.basename(filename);
  function key(id) {
    const index = id >>> 24;
    if (index > masters.length) throw Error('GRUP parent FormID has an invalid master index');
    return (id & 0xFFFFFF).toString(16) + ':' + (index < masters.length ? masters[index] : own).toLowerCase();
  }
  const fd = fs.openSync(filename, 'r'), records = new Map();
  try {
    const endFile = fs.fstatSync(fd).size;
    function walk(start, end, route, depth) {
      if (depth > 32) throw Error('Excessive GRUP nesting');
      let pos = start;
      while (pos < end) {
        const raw = Buffer.alloc(24);
        if (pos + 24 > end || fs.readSync(fd, raw, 0, 24, pos) !== 24) throw Error('Truncated record header');
        const sig = raw.toString('ascii', 0, 4), size = raw.readUInt32LE(4);
        const next = pos + (sig === 'GRUP' ? size : size + 24);
        if (next > end || next <= pos || (sig === 'GRUP' && size < 24)) throw Error('Invalid record bounds');
        if (sig === 'GRUP') {
          const type = raw.readInt32LE(12);
          if (type < 0 || type > 10) throw Error('Unmodeled GRUP type ' + type);
          const label = [1, 6, 7, 8, 9, 10].includes(type) ? key(raw.readUInt32LE(8)) : raw.subarray(8, 12).toString('hex');
          walk(pos + 24, next, [...route, {type, label}], depth + 1);
        } else if (sig !== 'TES4') {
          const id = raw.readUInt32LE(12);
          if (records.has(id)) throw Error('Duplicate raw record identity');
          records.set(id, {signature: sig, route});
        }
        pos = next;
      }
    }
    walk(0, endFile, [], 0);
    return records;
  } finally { fs.closeSync(fd); }
}

function recordBody(body, signature) {
  if (Object.prototype.hasOwnProperty.call(body, 'Child Group')) {
    if (!['WRLD', 'CELL', 'DIAL'].includes(signature) || !body['Child Group'] || typeof body['Child Group'] !== 'object')
      throw Error('Unmodeled child-group JSON');
    const result = {...body};
    delete result['Child Group'];
    return result;
  }
  return body;
}
// xEdit can render a VMAD reference with a master's declared casing or its
// on-disk casing after a dependency is added. Those identify the same plugin.
// Normalize only explicit FormID fields containing a whole qualified identity;
// dialogue, paths, names, arbitrary strings and the actual numeric ID stay checked.
function canonicalReferenceCase(value, key = '') {
  if (Array.isArray(value)) return value.map(item => canonicalReferenceCase(item, key));
  if (value && typeof value === 'object')
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, canonicalReferenceCase(v, k)]));
  if (key === 'FormID' && typeof value === 'string' && /^[^:\r\n]+\.(?:esm|esp|esl):[0-9a-f]{6,8}$/i.test(value))
    return value.toLowerCase();
  return value;
}
function parseErrorReports(entries) {
  const errors = [], warnings = [];
  for (const entry of entries) {
    const report = JSON.parse(entry);
    if (!report || !Array.isArray(report.errors)) throw Error('Unknown xEdit error report shape');
    for (const finding of report.errors) {
      // xeErrors.pas: groups 0/1 are ITM/ITPO, which can be intentional for a forwarded override.
      // Preserve them as warnings; every other/unknown group still blocks publication.
      (finding.group === 0 || finding.group === 1 ? warnings : errors).push(JSON.stringify(finding));
    }
  }
  return {errors, warnings};
}

async function main() {
  let x;
  try { x = require('xeditlib'); }
  catch (_) { x = require(path.join(base, 'tools/xelib/xelib')); }
  x.init(); x.setLanguage('English'); x.setGamePath(game + path.sep); x.setGameMode(x.GM_SSE);
  x.loadPlugins(plugin, true, false);
  await x.waitForLoader();
  const file = x.fileByName(plugin);
  const locations = recordLocations(path.join(game, 'Data', plugin));
  const records = {};
  const lightFlags = new Map();
  function isLight(name) {
    if (!lightFlags.has(name)) {
      const descriptor = fs.openSync(path.join(game, 'Data', name), 'r');
      const header = Buffer.alloc(12);
      try {
        if (fs.readSync(descriptor, header, 0, 12, 0) !== 12) throw Error('Truncated plugin header');
      } finally { fs.closeSync(descriptor); }
      lightFlags.set(name, name.toLowerCase().endsWith('.esl') || !!(header.readUInt32LE(8) & 0x200));
    }
    return lightFlags.get(name);
  }
  for (const r of x.getRecords(file, '', true)) {
    const master = x.getMasterRecord(r) || r;
    const defining = x.name(x.getElementFile(master));
    const local = (x.getFormID(master, false) & (isLight(defining) ? 0xFFF : 0xFFFFFF)).toString(16).toLowerCase();
    const key = `${local}:${defining.toLowerCase()}`;
    if (Object.prototype.hasOwnProperty.call(records, key)) throw Error('Duplicate record key ' + key);
    const body = JSON.parse(x.elementToJson(r));
    const id = x.getFormID(r, true), location = locations.get(id);
    if (!location || location.signature !== x.signature(r)) throw Error('xEdit/raw record inventory mismatch');
    locations.delete(id);
    records[key] = crypto.createHash('sha256').update(JSON.stringify({
      body: canonicalReferenceCase(recordBody(body, location.signature)), route: location.route
    })).digest('hex');
  }
  if (locations.size) throw Error('xEdit omitted raw records');
  x.checkForErrors(file);
  const until = Date.now() + 60000;
  while (!x.getErrorThreadDone()) {
    if (Date.now() > until) throw Error('xEdit reference check timed out');
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  const {errors, warnings} = parseErrorReports(x.getErrors());
  fs.writeFileSync(output, JSON.stringify({complete:true, records, errors, warnings, count:Object.keys(records).length}, null, 2));
  console.log('RESULT: OK');
}
module.exports = {parseErrorReports, recordLocations, recordBody, canonicalReferenceCase};
if (require.main === module) main().catch(e => { console.error(String(e)); process.exitCode = 1; });
