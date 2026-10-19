'use strict';
const mod = Process.getModuleByName('PengfeiChess.exe');
const directLogPath = "__PF_LOCAL_LOG__";
const fitSeedLog = directLogPath;
function emit(event, extra) { send(Object.assign({event}, extra || {})); }
function requireBytes(rva, expected) {
  const got = Array.from(new Uint8Array(mod.base.add(rva).readByteArray(expected.length / 2)))
    .map(x => x.toString(16).padStart(2, '0')).join('');
  if (got !== expected) throw new Error('instruction mismatch at 0x' + rva.toString(16) + ': ' + got);
}
// Validate every native entry before installing either process-local change.
requireBytes(0x88f10, '555657534881ecb8000000488dac248000000048c74530feffffff4889d64889');
requireBytes(0x52a0, 'e92ba40500');
requireBytes(0x1d29f0, '555657534881ec38010000488dac248000000048c785a8000000feffffff8b4118');
requireBytes(0x150f06, 'f30f108dd8050000');
requireBytes(0x15322a, '66490f6ec5f20f5ac0');
requireBytes(0x14e2f0, '4883ec7848894c24304889542438');
requireBytes(0x14e370, '5541574156415541545657534881ec18');
const backgroundAuthEntry = mod.base.add(0x1d29f0);
const nativeSleep = Process.getModuleByName('kernel32.dll').getExportByName('Sleep');
const rustAlloc = new NativeFunction(mod.base.add(0x52a0), 'pointer', ['uint64','uint64']);
function rustCString(text) {
  const p = rustAlloc(text.length + 1, 1);
  if (p.isNull()) throw new Error('Rust allocation failed');
  p.writeUtf8String(text); return p;
}
const localAuth = new NativeCallback(function(authOut, errorOut) {
  authOut.writePointer(rustCString('{"ClientCheckInfoItemList":[]}'));
  errorOut.writePointer(rustCString(''));
}, 'void', ['pointer','pointer']);
Interceptor.replace(mod.base.add(0x88f10), localAuth);
Interceptor.flush();
emit('local_auth_replacement_ready', {rva:'0x88f10', allocator:'0x52a0', directLogPath});

// This Rust background loop never returns. Preserve that contract by tail-jumping
// to Sleep(INFINITE); a return stub could reach invalid caller cleanup state.
Memory.patchCode(backgroundAuthEntry, 17, code => {
  const writer = new X86Writer(code, {pc: backgroundAuthEntry});
  writer.putMovRegU32('ecx', 0xffffffff);
  writer.putMovRegAddress('rax', nativeSleep);
  writer.putJmpReg('rax');
  writer.flush();
});

// Local diagnostic configuration: identical decoded byte arrays make their
// XOR neutral. Validate the original decoder before publishing either cell.
// This is a test fixture, not a recovered server configuration.
const decodeCoordinateConfig = new NativeFunction(mod.base.add(0x1856e0), 'pointer', ['pointer','pointer','uint64']);
let coordinateConfigDone = false;
Interceptor.attach(mod.base.add(0x14e2f0), {onEnter() {
  if (coordinateConfigDone) return;
  try {
    const config = 'WO' + 'AA'.repeat(32);
    const text = Memory.allocUtf8String(config), decoded = Memory.alloc(48);
    decoded.writeByteArray(new Uint8Array(48));
    decodeCoordinateConfig(decoded, text, config.length);
    const parts = [0,24].map(offset => {
      const part = decoded.add(offset), length = part.add(16).readU64().toNumber();
      if (length !== 32) throw new Error('Unexpected coordinate config length');
      return Array.from(new Uint8Array(part.add(8).readPointer().readByteArray(length)));
    });
    if (!parts[0].every((value, i) => value === 65 && value === parts[1][i])) throw new Error('Coordinate config decoder mismatch');
    for (const offset of [0,24]) {
      const part = decoded.add(offset);
      rustFree(part.add(8).readPointer(), part.readU64(), 1);
    }
    const snapshots = [];
    for (const rva of [0x7188740,0x7188748]) {
      const cell = mod.base.add(rva).readPointer();
      const state = cell.add(16).readU64().toNumber();
      const inner = cell.add(8).readPointer();
      let length = 0;
      if (state === 3 && !inner.isNull()) length = inner.add(16).readU64().toNumber();
      if (length > 0) { snapshots.push({slot:rva.toString(16),state,length,reused:true}); continue; }
      if (state !== 0 && (state !== 3 || cell.readU8() !== 0)) throw new Error('Coordinate cell is busy');
      const replacement = fitSeedAlloc(24,8);
      writeOwnedString(replacement, config);
      cell.add(8).writePointer(replacement); cell.writeU64(0); cell.add(16).writeU64(3);
      snapshots.push({slot:rva.toString(16),stateBefore:state,lengthBefore:length,lengthAfter:config.length});
    }
    coordinateConfigDone = true;
    fitSeedRecord({event:'coordinate_config_seeded',nativeDecodeVerified:true,xorBytes:32,xorValue:0,cells:snapshots,diagnosticFixture:true});
  } catch (e) { fitSeedRecord({event:'coordinate_config_seed_error',error:String(e)}); }
}});
emit('main_background_auth_suspended', {
  rva: '0x1d29f0', sleep: nativeSleep.toString(), wait_ms: 0xffffffff,
  patch_bytes: Array.from(new Uint8Array(backgroundAuthEntry.readByteArray(17)))
    .map(x => x.toString(16).padStart(2, '0')).join('')
});

// Current cells are 24 bytes: lock/value at +0, vector at +8, state at +16.
// The qword at +24 belongs to the next cell and must be left untouched.
const fitSeedAlloc = new NativeFunction(mod.base.add(0x52a0), 'pointer', ['uint64','uint64']);
let fitSeedDone = false;
function fitSeedRecord(value) {
  try { const f = new File(fitSeedLog, 'a'); f.write(JSON.stringify(Object.assign({eventTime:Date.now()},value))+'\n'); f.flush(); f.close(); } catch (_) {}
  send(value);
}
function seedVectorCell(globalRva, name) {
  const cell = mod.base.add(globalRva).readPointer();
  const state = cell.add(16).readU64().toNumber();
  let inner = ptr(0);
  try { inner = cell.add(8).readPointer(); } catch (_) {}
  let length = 0;
  try { if (!inner.isNull()) length = inner.add(16).readU64().toNumber(); } catch (_) {}
  if (state === 3 && !inner.isNull() && length > 0) return {name, globalRva:'0x'+globalRva.toString(16), state, length, reused:true};
  const data = fitSeedAlloc(16, 8);
  if (data.isNull()) throw new Error(name+' data allocation failed');
  data.writeDouble(1.0); data.add(8).writeDouble(0.0);
  const vec = fitSeedAlloc(24, 8);
  if (vec.isNull()) throw new Error(name+' vector allocation failed');
  vec.writeU64(2); vec.add(8).writePointer(data); vec.add(16).writeU64(2);
  cell.add(8).writePointer(vec); cell.writeU64(0); cell.add(16).writeU64(3);
  return {name, globalRva:'0x'+globalRva.toString(16), stateBefore:state, lengthBefore:length, stateAfter:3, lengthAfter:2, coefficients:[1,0]};
}
Interceptor.attach(mod.base.add(0x14e2f0), {onEnter() {
  if (fitSeedDone) return;
  fitSeedDone = true;
  try { fitSeedRecord({event:'fit_vectors_seeded',vectors:[seedVectorCell(0x7188730,'vec1'),seedVectorCell(0x7188738,'vec2')],source:'current Once-cell ABI'}); }
  catch (e) { fitSeedRecord({event:'fit_vectors_seed_error',error:String(e)}); }
}});
fitSeedRecord({event:'fit_vector_seed_ready',fitRva:'0x14e2f0',globals:['0x7188730','0x7188738'],functionalVerification:false});

// The current build keeps the detector label mapping in a HashMap Once cell.
// The smart/fixed connection scheme reaches this cache before the numeric fit
// vectors.  Populate it with the FEN identities used by the classifier.
const nativeStringHash = new NativeFunction(mod.base.add(0x173680), 'uint64', ['pointer','pointer']);
const cloneRawTable = new NativeFunction(mod.base.add(0x14d770), 'pointer', ['pointer','pointer']);
const rustFree = new NativeFunction(mod.base.add(0x52b0), 'void', ['pointer','uint64','uint64']);
const labelMap = {
  "0":"0","1":"n","2":"b","3":"a","4":"k","5":"r","6":"c","7":"p",
  "8":"R","9":"N","10":"A","11":"K","12":"B","13":"C","14":"P","15":"15",
  "d":"d","D":"D","r":"r","n":"n","b":"b","a":"a","k":"k","c":"c","p":"p",
  "R":"R","N":"N","B":"B","A":"A","K":"K","C":"C","P":"P"
};
const labelEntries = Object.entries(labelMap);
function writeOwnedString(at, text) {
  const bytes = Array.from(text).map(c => c.charCodeAt(0));
  const data = fitSeedAlloc(bytes.length, 1);
  if (data.isNull()) throw new Error('label allocation failed');
  data.writeByteArray(bytes); at.writeU64(bytes.length); at.add(8).writePointer(data); at.add(16).writeU64(bytes.length);
}
function readOwnedString(at) {
  const n = at.add(16).readU64().toNumber();
  if (n > 64) throw new Error('unexpected label length');
  return at.add(8).readPointer().readUtf8String(n);
}
function enumerateTable(table) {
  const mask = table.add(8).readU64().toNumber(), control = table.readPointer();
  if (mask > 1023) throw new Error('unexpected table mask');
  const out = {};
  for (let i = 0; i <= mask; i++) if (control.add(i).readU8() < 128) {
    const entry = control.sub(48 * (i + 1));
    out[readOwnedString(entry)] = readOwnedString(entry.add(24));
  }
  return out;
}
function freeRawTable(table) {
  const mask = table.add(8).readU64().toNumber(), control = table.readPointer();
  if (!mask) return;
  if (mask > 1023) throw new Error('unexpected table size');
  for (let i = 0; i <= mask; i++) if (control.add(i).readU8() < 128) {
    const entry = control.sub(48 * (i + 1));
    for (const offset of [0, 24]) {
      const str = entry.add(offset), cap = str.readU64().toNumber();
      if (cap) rustFree(str.add(8).readPointer(), cap, 1);
    }
  }
  rustFree(control.sub(48 * (mask + 1)), 48 * (mask + 1) + mask + 17, 16);
}
function makeLabelTable(seed) {
  const bucketCount = 64, mask = bucketCount - 1, total = bucketCount * 48 + bucketCount + 16;
  const storage = fitSeedAlloc(total, 16);
  if (storage.isNull()) throw new Error('table allocation failed');
  storage.writeByteArray(new Uint8Array(total));
  const table = Memory.alloc(48), control = storage.add(bucketCount * 48);
  table.writeByteArray(new Uint8Array(48)); table.writePointer(control); table.add(8).writeU64(mask);
  table.add(16).writeU64(56 - labelEntries.length); table.add(24).writeU64(labelEntries.length);
  table.add(32).writeByteArray(seed.readByteArray(16));
  control.writeByteArray(new Uint8Array(bucketCount + 16).fill(255));
  for (const [letter, value] of labelEntries) {
    const key = Memory.alloc(24); writeOwnedString(key, letter);
    const hash = nativeStringHash(table.add(32), key);
    let slot = hash.and(mask).toNumber(), step = 0, chosen = -1;
    while (chosen < 0) {
      for (let j = 0; j < 16; j++) { const i = (slot + j) & mask; if (control.add(i).readU8() === 255) { chosen = i; break; } }
      if (chosen < 0) { step += 16; slot = (slot + step) & mask; if (step > bucketCount) throw new Error('table full'); }
    }
    const entry = control.sub(48 * (chosen + 1));
    entry.writeByteArray(key.readByteArray(24)); writeOwnedString(entry.add(24), value);
    const h2 = hash.shr(57).toNumber(); control.add(chosen).writeU8(h2);
    if (chosen < 16) control.add(bucketCount + chosen).writeU8(h2);
  }
  return table;
}
let labelSeedDone = false;
Interceptor.attach(mod.base.add(0x14e46a), { onEnter() {
  if (labelSeedDone) return;
  try {
    const cell = mod.base.add(0x7188728).readPointer(), table = this.context.rdi;
    if (!cell.add(8).readPointer().equals(table) || cell.readU8() !== 1) throw new Error('label cache lock/pointer mismatch');
    if (table.add(24).readU64().toNumber() !== 0) { labelSeedDone = true; return; }
    if (table.add(8).readU64().toNumber() !== 0) throw new Error('unexpected preallocated label table');
    const fresh = makeLabelTable(table.add(32));
    const cloned = Memory.alloc(48); cloned.writeByteArray(new Uint8Array(48)); cloneRawTable(cloned, fresh);
    const values = enumerateTable(cloned);
    if (Object.keys(values).length !== labelEntries.length || !labelEntries.every(([k,v]) => values[k] === v)) throw new Error('native label clone mismatch');
    freeRawTable(cloned);
    table.writeByteArray(fresh.readByteArray(48)); labelSeedDone = true;
    fitSeedRecord({event:'fit_label_cache_seeded', globalRva:'0x7188728', entries:values, nativeCloneVerified:true, functionalVerification:false});
  } catch (e) { fitSeedRecord({event:'fit_label_cache_error', globalRva:'0x7188728', error:String(e)}); }
}});

// Keep the rectangle computed by the original native geometric fitter.
// The subsequent coordinate representation round-trip depends on unfilled
// configuration; its invalid values must not replace a valid fitted rectangle.
// Pair state with each actual inner-fit invocation, including nested calls.
const fitRectScopes = new Map();
Interceptor.attach(mod.base.add(0x14e370), {
  onEnter() {
    this.tid=Process.getCurrentThreadId();
    const stack=fitRectScopes.get(this.tid)||[];
    stack.push({rect:null});fitRectScopes.set(this.tid,stack);
  },
  onLeave() {
    const stack=fitRectScopes.get(this.tid);
    if(stack) {stack.pop();if(!stack.length)fitRectScopes.delete(this.tid);}
  }
});
Interceptor.attach(mod.base.add(0x150f06), {onEnter() {
  const stack=fitRectScopes.get(Process.getCurrentThreadId());
  if(!stack||!stack.length)return;
  const rect=[0,4,8,12].map(offset=>this.context.rbp.add(0x5d8+offset).readFloat());
  if(rect.every(Number.isFinite)&&rect[2]>0&&rect[3]>0) stack[stack.length-1].rect=rect;
}});
let restoredRectCount=0;
Interceptor.attach(mod.base.add(0x15322a), {onEnter() {
  const stack=fitRectScopes.get(Process.getCurrentThreadId());
  if(!stack||!stack.length||!stack[stack.length-1].rect)return;
  const rect=stack[stack.length-1].rect, bytes=Memory.alloc(32);
  const names=['r13','r12','rsi','rdi'];
  rect.forEach((v,i)=>{bytes.add(8*i).writeDouble(v);this.context[names[i]]=bytes.add(8*i).readPointer();});
  if(++restoredRectCount<=20||restoredRectCount%100===0)
    fitSeedRecord({event:'fit_geometric_rect_preserved',number:restoredRectCount,rect,
      sourceRva:'0x150f06',destinationRva:'0x15322a'});
}});

// The fitted JSON preserves detector labels, while ComputeChessSituation
// expects single-character chess symbols. Adapt the two borrowed UTF-8 inputs;
// never modify managed/interned strings or change ownership of native outputs.
const detectorSymbols = Object.freeze({
  '1':'n','2':'b','3':'a','4':'k','5':'r','6':'c','7':'p',
  '8':'R','9':'N','10':'A','11':'K','12':'B','13':'C','14':'P'
});
let adaptedFitCount=0;
Interceptor.attach(mod.base.add(0x14e2f0), {
  onEnter(args) {
    this.labelInputBuffers=[];
    try {
      // Prepare both buffers before publishing either, so a parse failure leaves
      // both inputs unchanged. References remain alive for the entire call.
      const prepared=[0,1].map(index=>{
        const raw=args[index].readUtf8String(), items=JSON.parse(raw), changes={};
        if(!Array.isArray(items))throw new Error('Expected prediction array');
        for(const item of items) {
          if(!item || typeof item.label_name!=='string')throw new Error('Invalid prediction label');
          const before=item.label_name;
          if(Object.prototype.hasOwnProperty.call(detectorSymbols,before)) {
            item.label_name=detectorSymbols[before];changes[before]=(changes[before]||0)+1;
          }
        }
        const changed=Object.keys(changes).length>0;
        return {index,count:items.length,changes,buffer:changed?Memory.allocUtf8String(JSON.stringify(items)):null};
      });
      for(const entry of prepared) if(entry.buffer) {
        this.labelInputBuffers.push(entry.buffer);args[entry.index]=entry.buffer;
      }
      if(++adaptedFitCount<=20||adaptedFitCount%100===0)
        fitSeedRecord({event:'fit_input_labels_adapted',number:adaptedFitCount,
          inputs:prepared.map(({index,count,changes})=>({index,count,changes}))});
    }catch(e){fitSeedRecord({event:'fit_input_labels_error',error:String(e)});}
  },
  onLeave() {this.labelInputBuffers=null;}
});
fitSeedRecord({event:'fit_input_label_adapter_ready',symbols:detectorSymbols,borrowedInputs:true});

// Restore only a non-finite classifier score from its own verified raw output.
// The model's label, class index, raw vector, and consumer thresholds are retained.
(() => {
  const base = Process.getModuleByName('PengfeiChess.exe').base;
  const logfile = fitSeedLog;
  let calls = 0, repaired = 0, rejected = 0, consumed = 0, kings = 0;
  function record(event, data) {
    try {
      const value = {eventTime: Date.now(), event, pid: Process.id,
        tid: Process.getCurrentThreadId(), ...data};
      const f = new File(logfile, 'a');
      f.write(JSON.stringify(value) + '\n'); f.flush(); f.close(); send(value);
    } catch (_) { /* Logging must not unwind into the native caller. */ }
  }
  function check(rva, expected) {
    const actual = Array.from(new Uint8Array(base.add(rva).readByteArray(expected.length / 2)))
      .map(x => x.toString(16).padStart(2, '0')).join('');
    if (actual !== expected) throw new Error('Classifier signature mismatch at ' + rva.toString(16));
  }
  function str(p) {
    if (p.isNull()) return null;
    const n = p.add(8).readS32();
    if (n < 0 || n > 32) throw new Error('Invalid classifier label length');
    return p.add(12).readUtf16String(n);
  }
  function hex32(n) { return '0x' + n.toString(16).padStart(8, '0'); }
  function inspect(p) {
    if (p.isNull()) throw new Error('Null classifier result');
    const id = p.add(24).readS32(), label = str(p.add(8).readPointer());
    const list = p.add(16).readPointer();
    if (list.isNull() || list.add(16).readS32() !== 19) throw new Error('Expected 19 raw scores');
    const array = list.add(8).readPointer();
    if (array.isNull() || array.add(8).readU32() < 19 || array.add(8).readU32() > 256)
      throw new Error('Invalid classifier backing array');
    const raw = [];
    for (let i = 0; i < 19; i++) raw.push(array.add(16 + 4 * i).readFloat());
    if (!raw.every(Number.isFinite)) throw new Error('Non-finite raw network output');
    let argmax = 0;
    for (let i = 1; i < raw.length; i++) if (raw[i] > raw[argmax]) argmax = i;
    if (id !== argmax) throw new Error('Class index does not match native argmax');
    if (!label || !/^(?:[rnbakcpRNBAKCPDd]|blank)$/.test(label))
      throw new Error('Unexpected classifier label');
    return {id, label, raw, argmax, maximum: raw[argmax]};
  }
  check(0xc5a61a, '488bc30f28b42490000000');
  check(0xccb630, '488bd7488bcbe80567ceff');
  // All signature checks precede installation.
  Interceptor.attach(base.add(0xc5a61a), {onEnter() {
    calls++;
    try {
      const p = this.context.rbx;
      if (p.isNull()) return;
      const slot = p.add(28), before = slot.readFloat();
      if (Number.isFinite(before)) return;
      const bits = slot.readU32(), result = inspect(p);
      // One scalar write; no allocations or modifications in managed objects.
      slot.writeFloat(result.maximum);
      const after = slot.readFloat();
      repaired++;
      if (repaired <= 60 || repaired % 200 === 0) record('classifier_score_restored', {
        calls, repaired, originalBits: hex32(bits), originalKind: (bits & 0x7fffffff) > 0x7f800000 ? 'NaN' : (bits >>> 31 ? '-Infinity' : 'Infinity'),
        ...result, restored: after, restoredBits: hex32(slot.readU32())
      });
    } catch (e) {
      rejected++;
      if (rejected <= 20 || rejected % 100 === 0)
        record('classifier_score_repair_rejected', {calls, rejected, error: String(e)});
    }
  }});
  // Observe the existing threshold decision, without replacing its result.
  Interceptor.attach(base.add(0xccb630), {onEnter() {
    consumed++;
    if (consumed > 100 && consumed % 200 !== 0) return;
    try {
      const p = this.context.rsi, piece = this.context.rdi;
      record('classifier_piece_decision', {number: consumed,
        label: str(p.add(8).readPointer()), score: p.add(28).readFloat(),
        marginClassIndex: base.add(0x34e4a38).readS32(),
        piece: piece.isNull() ? null : {color: piece.add(8).readS32(), kind: piece.add(12).readS32(), covered: piece.add(16).readU8()}
      });
    } catch (e) { record('classifier_piece_decision_read_error', {error: String(e)}); }
  }});
  Interceptor.attach(base.add(0xccfba0), {onLeave(value) {
    kings++;
    if (kings <= 50 || kings % 100 === 0)
      record('trace_king_search_result', {number: kings, kings: value.toInt32()});
  }});
  record('classifier_score_repair_ready', {rva: '0xc5a61a', nonFiniteOnly: true,
    expectedRawCount: 19, source: 'native argmax raw score', thresholdsUnchanged: true});
})();
