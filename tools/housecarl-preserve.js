// Retain existing-plugin header identity/dependencies in a detached transaction only.
// All record/reference checks still run independently after this step.
'use strict';
const fs = require('fs');
const path = require('path');
function header(filename) {
  const fd = fs.openSync(filename, 'r');
  try {
    const raw = Buffer.alloc(24);
    if (fs.readSync(fd, raw, 0, 24, 0) !== 24 || raw.toString('ascii', 0, 4) !== 'TES4') throw Error('Invalid TES4 header');
    const size = raw.readUInt32LE(4);
    if (size > 16 * 1024 * 1024) throw Error('Unreasonable TES4 header');
    const body = Buffer.alloc(size);
    if (fs.readSync(fd, body, 0, size, 24) !== size) throw Error('Truncated TES4 header');
    const masters = [];
    let version, offset = 0, extended = null;
    while (offset < size) {
      if (offset + 6 > size) throw Error('Truncated TES4 subrecord');
      const sig = body.toString('ascii', offset, offset + 4);
      let length = body.readUInt16LE(offset + 4); offset += 6;
      if (sig === 'XXXX') {
        if (length !== 4 || offset + 4 > size || extended !== null) throw Error('Invalid extended subrecord');
        extended = body.readUInt32LE(offset); offset += 4; continue;
      }
      if (extended !== null) { length = extended; extended = null; }
      if (offset + length > size) throw Error('Truncated TES4 payload');
      if (sig === 'MAST') masters.push(new TextDecoder('windows-1252').decode(body.subarray(offset, offset + length)).replace(/\0+$/, ''));
      if (sig === 'HEDR') {
        if (length !== 12 || version !== undefined) throw Error('Invalid HEDR');
        version = body.readFloatLE(offset);
      }
      offset += length;
    }
    if (extended !== null || !Number.isFinite(version)) throw Error('Incomplete HEDR');
    return {masters, version, flags: raw.readUInt32LE(8)};
  } finally { fs.closeSync(fd); }
}

async function main() {
  const [request, output] = process.argv.slice(2);
  const spec = JSON.parse(fs.readFileSync(request, 'utf8'));
  const original = header(spec.original), before = header(spec.candidate);
  if (original.flags !== before.flags) throw Error('Writer changed plugin flags');
  const name = path.basename(spec.candidate);
  function recordIds(filename) {
    const fd = fs.openSync(filename, 'r'), ids = [];
    try {
      const endFile = fs.fstatSync(fd).size;
      function walk(start, end, depth) {
        if (depth > 32) throw Error('Excessive group nesting');
        let pos = start;
        while (pos < end) {
          const h = Buffer.alloc(24);
          if (pos + 24 > end || fs.readSync(fd, h, 0, 24, pos) !== 24) throw Error('Truncated record header');
          const sig = h.toString('ascii', 0, 4), size = h.readUInt32LE(4);
          const next = pos + (sig === 'GRUP' ? size : size + 24);
          if (next > end || next <= pos || (sig === 'GRUP' && size < 24)) throw Error('Invalid record bounds');
          if (sig === 'GRUP') walk(pos + 24, next, depth + 1);
          else if (sig !== 'TES4') ids.push(sig + ':' + h.readUInt32LE(12));
          pos = next;
        }
      }
      walk(0, endFile, 0);
      return ids.sort();
    } finally { fs.closeSync(fd); }
  }
  const detached = path.join(spec.game, 'Data', name);
  if (fs.statSync(detached).nlink !== 1 || fs.statSync(spec.candidate).nlink !== 1) throw Error('Candidate is not detached');
  const wanted = new Set([...original.masters, ...before.masters].map(n => n.toLowerCase()));
  const load = spec.order.filter(n => wanted.has(n.toLowerCase()) && n.toLowerCase() !== name.toLowerCase());
  if (new Set(load.map(n => n.toLowerCase())).size !== wanted.size) throw Error('A required master is outside the snapshot');
  const base = process.env.HOUSECARL_XELIB_ROOT;
  const x = require(path.join(base, 'tools/xelib/xelib'));
  x.init(); x.setLanguage('English'); x.setGamePath(spec.game + path.sep); x.setGameMode(x.GM_SSE);
  const effectiveDataPath = x.getGlobal('DataPath');
  const canonical = p => fs.realpathSync.native(path.resolve(p)).toLowerCase();
  if (canonical(effectiveDataPath) !== canonical(path.join(spec.game, 'Data')))
    throw Error('Effective xEdit DataPath differs from private candidate view');
  x.loadPlugins([...load, name].join('\n'), true, false);
  await x.waitForLoader();
  const file = x.fileByName(name);
  const loadedMasters = x.getMasterNames(file);
  if (JSON.stringify(loadedMasters.map(n => n.toLowerCase())) !== JSON.stringify(before.masters.map(n => n.toLowerCase())))
    throw Error('Loaded master list differs from detached candidate');
  // Keep a semantic inventory across the xEdit save, including requested new records.
  const inventory = () => x.getRecords(file, '', true).map(r => ({
    id: x.signature(r) + ':' + x.getFormID(r, true), body: JSON.parse(x.elementToJson(r))
  }));
  const loadedBefore = inventory();
  if (JSON.stringify(loadedBefore.map(r => r.id).sort()) !== JSON.stringify(recordIds(detached)))
    throw Error('Loaded record identities differ from detached candidate');
  const present = new Set(x.getMasterNames(file).map(n => n.toLowerCase()));
  const added = original.masters.filter(n => !present.has(n.toLowerCase()));
  for (const master of added) x.addMaster(file, master);
  x.sortMasters(file);
  x.setFloatValue(file, 'File Header\\HEDR\\Version', original.version);
  const loadedAfter = inventory();
  // Adding/sorting masters changes disk-local indices, so compare the resolved JSON bodies.
  if (JSON.stringify(loadedBefore.map(r => r.body)) !== JSON.stringify(loadedAfter.map(r => r.body))) {
    const differences = [];
    function compare(a, b, field) {
      if (JSON.stringify(a) === JSON.stringify(b) || differences.length >= 20) return;
      if (a && b && typeof a === 'object' && typeof b === 'object') {
        for (const k of new Set([...Object.keys(a), ...Object.keys(b)])) compare(a[k], b[k], field + '/' + k);
      } else differences.push({field, before:a, after:b});
    }
    compare(loadedBefore, loadedAfter, 'records');
    fs.writeFileSync(output + '.failure.json', JSON.stringify({complete:false, differences}, null, 2));
    throw Error('Header retention changed record content; diagnostics: ' + output + '.failure.json');
  }
  x.saveFile(file, spec.candidate);
  if (JSON.stringify(loadedAfter.map(r => r.id).sort()) !== JSON.stringify(recordIds(spec.candidate)))
    throw Error('Saved record identities differ from retained candidate');
  const after = header(spec.candidate);
  if (after.flags !== original.flags || after.version !== original.version ||
      [...wanted].some(n => !after.masters.some(m => m.toLowerCase() === n))) throw Error('Header readback mismatch');
  fs.writeFileSync(output, JSON.stringify({complete: true, added_masters: added, before, after,
    effective_data_path: effectiveDataPath, records_before: loadedBefore.length, records_after: loadedAfter.length}, null, 2));
  console.log('RESULT: OK');
}
module.exports = {header};
if (require.main === module) main().catch(e => { console.error(String(e)); process.exitCode = 1; });
