'use strict';
// Controls for tools/housecarl-verify.js and tools/housecarl-preserve.js on synthetic plugin bytes (no xEdit, no game
// data). Ported from the install's reviewed verifier controls (2026-10-28) plus the reference-casing rule.
// Usage: node housecarl-verify-controls.js <tools dir> <scratch dir>
const fs = require('fs'), path = require('path'), assert = require('assert');
const [tools, dir] = process.argv.slice(2);
const {recordLocations, recordBody, parseErrorReports, canonicalReferenceCase} = require(path.join(tools, 'housecarl-verify'));
const {header} = require(path.join(tools, 'housecarl-preserve'));
function record(sig, id, body = Buffer.alloc(0)) {
  const h = Buffer.alloc(24); h.write(sig); h.writeUInt32LE(body.length, 4); h.writeUInt32LE(id, 12);
  return Buffer.concat([h, body]);
}
function group(type, label, children) {
  const body = Buffer.concat(children), h = Buffer.alloc(24); h.write('GRUP'); h.writeUInt32LE(24 + body.length, 4);
  if (typeof label === 'number') h.writeUInt32LE(label, 8); else h.write(label, 8);
  h.writeInt32LE(type, 12); return Buffer.concat([h, body]);
}
function tes4(masters = []) {
  const hedr = Buffer.alloc(18); hedr.write('HEDR'); hedr.writeUInt16LE(12, 4); hedr.writeFloatLE(1.7, 6);
  const parts = [hedr];
  for (const m of masters) { const b = Buffer.from(m + '\0', 'latin1'), h = Buffer.alloc(6); h.write('MAST'); h.writeUInt16LE(b.length, 4); parts.push(h, b); }
  return record('TES4', 0, Buffer.concat(parts));
}
let passed = 0;
function test(name, fn) { fn(); ++passed; console.log(name + ': passed'); }
const file = path.join(dir, 'Test.esp');
function write(bytes) { fs.writeFileSync(file, bytes); }
test('CP1252 master decoding', () => { write(tes4(['Euro\x80.esp'])); assert.deepStrictEqual(header(file).masters, ['Euro€.esp']); });
test('child JSON excluded only at root', () => {
  const body = {'Record Header': {FormID: 9}, 'Child Group': {a: 1}, keep: {'Child Group': {x: 1}}};
  assert.deepStrictEqual(recordBody(body, 'WRLD'), {'Record Header': {FormID: 9}, keep: {'Child Group': {x: 1}}});
  assert.ok(body['Child Group']);
  assert.throws(() => recordBody(body, 'WEAP'), /Unmodeled/);
});
test('all child records independently inventoried', () => {
  write(Buffer.concat([tes4(['Skyrim.esm']), group(0, 'WRLD', [record('WRLD', 0x3c), group(1, 0x3c, [group(4, 0xffffffff, [group(5, 0xffffffff, [record('CELL', 0x01000800)])])])])]));
  const rows = recordLocations(file); assert.equal(rows.size, 2); assert.equal(rows.get(0x01000800).route.length, 4);
  assert.equal(rows.get(0x01000800).route[1].label, '3c:skyrim.esm');
});
test('reparenting changes child ancestry', () => {
  const make = parent => Buffer.concat([tes4(['Skyrim.esm']), group(0, 'WRLD', [group(1, parent, [record('CELL', 0x01000800)])])]);
  write(make(0x3c)); const a = recordLocations(file).get(0x01000800).route;
  write(make(0x3d)); assert.notDeepStrictEqual(recordLocations(file).get(0x01000800).route, a);
});
test('block relocation changes ancestry', () => {
  const make = block => Buffer.concat([tes4(), group(0, 'CELL', [group(2, block, [record('CELL', 0x800)])])]);
  write(make(1)); const a = recordLocations(file).get(0x800).route;
  write(make(2)); assert.notDeepStrictEqual(recordLocations(file).get(0x800).route, a);
});
test('master reindex preserves parent FormKey', () => {
  write(Buffer.concat([tes4(['Skyrim.esm', 'Dawnguard.esm']), group(0, 'WRLD', [group(1, 0x01000bb5, [record('CELL', 0x02000800)])])]));
  const a = recordLocations(file).get(0x02000800).route;
  write(Buffer.concat([tes4(['Skyrim.esm', 'Update.esm', 'Dawnguard.esm']), group(0, 'WRLD', [group(1, 0x02000bb5, [record('CELL', 0x03000800)])])]));
  assert.deepStrictEqual(recordLocations(file).get(0x03000800).route, a);
});
test('truncation refuses', () => { write(Buffer.concat([tes4(), Buffer.alloc(3)])); assert.throws(() => recordLocations(file), /Truncated/); });
test('duplicate identity refuses', () => { write(Buffer.concat([tes4(), record('CELL', 0x800), record('CELL', 0x800)])); assert.throws(() => recordLocations(file), /Duplicate/); });
test('unknown group refuses', () => { write(Buffer.concat([tes4(), group(11, 0, [])])); assert.throws(() => recordLocations(file), /Unmodeled/); });
test('out-of-range parent refuses', () => { write(Buffer.concat([tes4(['Skyrim.esm']), group(1, 0x02000800, [record('CELL', 0x01000800)])])); assert.throws(() => recordLocations(file), /invalid master index/); });
test('reference errors remain errors', () => {
  const result = parseErrorReports([JSON.stringify({errors: [{group: 4, data: 'bad'}, {group: 0, data: 'itm'}]})]);
  assert.equal(result.errors.length, 1); assert.equal(result.warnings.length, 1);
});
test('reference casing folds only whole FormID identities', () => {
  const body = {VMAD: {Scripts: [{FormID: 'Dawnguard.ESM:00ABCDEF', Name: 'KeepCase.esp:00ABCDEF'}]}, FormID: 'not a plugin ref'};
  assert.deepStrictEqual(canonicalReferenceCase(body),
    {VMAD: {Scripts: [{FormID: 'dawnguard.esm:00abcdef', Name: 'KeepCase.esp:00ABCDEF'}]}, FormID: 'not a plugin ref'});
  assert.notDeepStrictEqual(canonicalReferenceCase({FormID: 'a.esp:00000001'}), canonicalReferenceCase({FormID: 'a.esp:00000002'}));
});
console.log('Passed ' + passed + ' verifier controls');
