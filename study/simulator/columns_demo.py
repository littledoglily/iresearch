#!/usr/bin/env python3
"""Demo for the two column-writing structures:
 (1) columnstore2::column  (no comparator; streaming, direct)
 (2) BufferedColumn        (comparator; buffer -> remap by docmap -> write)
Uses helpers from sim.py / flush.py. Run: python3 columns_demo.py"""
import math, functools
from sim import vint, be32, be64, wstr, zz64, hx
from flush import header, MAGIC
from files import pack_table

def sparse_bitmap_bytes(docs):
    """sparse_bitmap_writer output for docs < 65536 (single block, <=4095 values) + eof sentinel block"""
    docs = sorted(docs); out = bytearray()
    out += (0).to_bytes(2, 'big') + (len(docs) - 1).to_bytes(2, 'big')
    for d in docs: out += d.to_bytes(2, 'big')
    out += (65535).to_bytes(2, 'big') + (0).to_bytes(2, 'big') + (65535).to_bytes(2, 'big')
    return bytes(out)

class DCol:
    """columnstore2::column (only the parts relevant to memory layout)"""
    def __init__(s, name, cid):
        s.name, s.cid = name, cid
        s.data = bytearray(); s.addr = []; s.bits = []; s.pend = 0; s.prev = 0
    def prepare(s, key):
        assert key > 0
        if key > s.pend:
            s.prev = s.pend; s.pend = key; s.bits.append(key); s.addr.append(len(s.data))
    def write(s, b): s.data += b
    def reset(s):
        if not s.addr: return
        s.bits.remove(s.pend); del s.data[s.addr[-1]:]; s.addr.pop(); s.pend = s.prev
    def snap(s): return dict(data=bytes(s.data), addr=list(s.addr), bits=list(s.bits), pend=s.pend, prev=s.prev)

def fmt_d(c):
    if c is None: return '(未创建)'
    return 'data_=[%s] (%dB) · addr_table_=%s · 文档位图 bits_=%s · pend_=%d · prev_=%d' % (
        hx(c['data']), len(c['data']), c['addr'], '{' + ','.join(map(str, c['bits'])) + '}', c['pend'], c['prev'])

def direct_demo():
    docs = {1: {'id': 'a1'}, 2: {'id': 'a2', 'tag': 'x'}, 3: {'id': 'a3', 'tag': 'hello'}, 4: {'id': 'a4'}, 5: {'id': 'a5', 'tag': 'yz'}}
    cols = {}; order = []; steps = []
    for d, fv in docs.items():
        for name, val in fv.items():
            before = {k: c.snap() for k, c in cols.items()}
            created = name not in cols
            if created:
                cols[name] = DCol(name, len(cols)); order.append(name)
            c = cols[name]; c.prepare(d); b = wstr(val); c.write(b)
            steps.append(dict(doc=d, name=name, val=val, bytes=b, created=created, cid=c.cid,
                              before=before.get(name), after=c.snap()))
    return cols, order, steps

def flush_direct(cols, order):
    """commit: finalize each column -> .csd blocks, then .csi (+ bitmaps appended to .csd while writing .csi)"""
    csd = bytearray(); rows = []
    def add(label, b, show=None):
        rows.append((len(csd), len(b), label, show or hx(b))); csd.extend(b)
    add('header', header('iresearch_11_columnstore_data', 0), 'magic · "iresearch_11_columnstore_data" · ver=0')
    blocks = {}
    for name in order:
        c = cols[name]; offs = list(c.addr); n = len(offs)
        base = offs[0]; end = offs[-1]; dist = n - 1
        avg = int(math.floor((end - base) / (dist if dist else 1) + 0.5))
        resid = [0]; ab = base
        for x in offs[1:]:
            ab += avg; resid.append(zz64(x - ab))
        all_eq = not any(resid)
        last_size = len(c.data) - offs[-1]
        blk = dict(addr=len(csd), avg=avg, resid=resid, all_eq=all_eq, last_size=last_size, n=n, offs=offs)
        if not all_eq:
            bits = max(resid).bit_length(); tb = (64 * bits + 7) // 8
            add('列 %s 偏移表' % name, pack_table(resid, bits))
            blk['bits'] = bits
        else: blk['bits'] = 0
        blk['data'] = base + len(csd)
        add('列 %s 值区' % name, bytes(c.data))
        # fixed-length detection (single block): all residuals zero and last value same size as avg
        blk['fixed'] = all_eq and last_size == avg
        blocks[name] = blk
    # .csi: sorted by name, finish() appends bitmaps to csd
    csi = bytearray(); csi_rows = []
    def cadd(label, b): csi_rows.append((len(csi), len(b), label, hx(b))); csi.extend(b)
    cadd('header', header('iresearch_11_columnstore_index', 0)); cadd('vint(列数)', vint(len(order)))
    for name in sorted(order):
        c = cols[name]; blk = blocks[name]
        docs_index = 0
        has_bitmap = not (c.bits[0] + len(c.bits) - 1 == c.bits[-1])  # hdr.min + docs_count - 1 != pend_
        if has_bitmap:
            docs_index = len(csd); bm = sparse_bitmap_bytes(c.bits)
            add('列 %s 稀疏位图(docs_)' % name, bm)
            blk['bitmap'] = bm
        ty = 2 if blk['fixed'] else 0
        cadd('列 %s: 压缩器名' % name, wstr('iresearch::compression::none'))
        cadd('列 %s: column_header{docs_index=%d,id=%d,min=%d,docs_count=%d,type=%s,props=kNormal}' % (name, docs_index, c.cid, c.bits[0], len(c.bits), 'kFixed' if blk['fixed'] else 'kSparse'),
             be64(docs_index) + be32(c.cid) + be32(c.bits[0]) + be32(len(c.bits)) + ty.to_bytes(2, 'big') + (0).to_bytes(2, 'big'))
        cadd('列 %s: string(payload)=空' % name, vint(0))
        cadd('列 %s: string(列名)' % name, wstr(name))
        if has_bitmap: cadd('列 %s: bitmap 索引 write_int(0)(块数 ≤ 2 不写块索引)' % name, be32(0))
        if blk['fixed']:
            cadd('列 %s: long(avg=%d)' % (name, blk['avg']), be64(blk['avg']))
            cadd('列 %s: 块 long(data=%d)' % (name, blk['data']), be64(blk['data']))
        else:
            cadd('列 %s: 块元数据 addr=%d avg=%d bits=%d data=%d last_size=%d' % (name, blk['addr'], blk['avg'], blk['bits'], blk['data'], blk['last_size']),
                 be64(blk['addr']) + be64(blk['avg']) + bytes([blk['bits']]) + be64(blk['data']) + be64(blk['last_size']))
    add('footer', be32(-MAGIC) + be32(0) + b'\0' * 8, 'footer')
    cadd('footer', be32(-MAGIC) + be32(0) + b'\0' * 8)
    return rows, csi_rows, blocks, len(csd), len(csi)

# ---------------------------------------------------------------- buffered side
DOCMAP = [0, 2, 4, 1, 5, 3]   # from the 2.8 example (old -> new)
def use_dense(size, total): return size * math.log(size) >= total

def buffered_demo():
    out = {}
    # (i) id column (dense path)
    ids = [(k, (k - 1) * 3, 3) for k in range(1, 6)]
    buf = [None] * 5
    for k, b, s in ids: nk = DOCMAP[k]; buf[nk - 1] = (nk, b, s, k)
    out['dense'] = dict(index=ids, buffer=buf)
    # (ii) sparse tag column  docs 2:"x" 3:"hello" 5:"yz"
    vals = [(2, wstr('x')), (3, wstr('hello')), (5, wstr('yz'))]
    data = b''.join(v for _, v in vals); idx = []; o = 0
    for k, v in vals: idx.append((k, o, len(v))); o += len(v)
    out['sparse_before'] = (data, list(idx), use_dense(3, 5))
    rekey = [(DOCMAP[k], b, s) for k, b, s in idx]
    srt = sorted(rekey, key=lambda t: t[0])
    out['sparse_rekey'] = rekey; out['sparse_after'] = srt
    return out

def primary_missing_demo():
    """sort column where docs 2 and 5 have no sort value; descending comparator"""
    vals = {1: 'pear', 3: 'plum', 4: 'apple'}
    idx = [(k, v) for k, v in sorted(vals.items())]
    ndocs = 5
    cmp = lambda l, r: (r[1] > l[1]) - (r[1] < l[1])
    order = sorted(range(len(idx)), key=functools.cmp_to_key(lambda a, b: cmp(idx[a], idx[b]) or (a > b) - (a < b)))
    docmap = [0] * (ndocs + 1); new_doc = 1; trace = []; written = []
    for i in order:
        key = idx[i][0]
        mn = 1 + (idx[i - 1][0] if i else 0)
        for m in range(mn, key):
            docmap[m] = new_doc; trace.append('旧 doc%d(无排序值)→新 doc%d' % (m, new_doc)); new_doc += 1
        docmap[key] = new_doc; trace.append('旧 doc%d(%s)→新 doc%d' % (key, idx[i][1], new_doc)); written.append((new_doc, idx[i][1])); new_doc += 1
    for m in range(new_doc, ndocs + 1):
        # remaining entries beyond last sorted doc (docmap index == new_doc position)
        pass
    tail = []
    # trailing docs: iterate docmap positions new_doc..end (C++: begin+new_doc ... end) ; they hold 0 => assign new_doc++
    j = new_doc
    while j <= ndocs:
        # find the old doc that still has docmap 0 with the smallest id
        old = next(k for k in range(1, ndocs + 1) if docmap[k] == 0)
        docmap[old] = new_doc; trace.append('旧 doc%d(排在最后一个有值文档之后,无值)→新 doc%d' % (old, new_doc)); new_doc += 1; j += 1
    return dict(order=order, docmap=docmap, trace=trace, written=written, idx=idx)

if __name__ == '__main__':
    cols, order, steps = direct_demo()
    for s in steps:
        print('doc%d %s=%r  col%d%s' % (s['doc'], s['name'], s['val'], s['cid'], ' (新建)' if s['created'] else ''))
        print('   before:', fmt_d(s['before'])); print('   after :', fmt_d(s['after']))
    rows, csi_rows, blocks, csd_len, csi_len = flush_direct(cols, order)
    print('--- csd', csd_len)
    for r in rows: print(r[:3], r[3][:70])
    print('--- csi', csi_len)
    for r in csi_rows: print(r[:3], r[3][:70])
    b = buffered_demo(); print(b)
    print(primary_missing_demo())
