#!/usr/bin/env python3
"""Byte-exact simulation of iresearch segment_writer (sorted/random-access mode, format 1_4)
for a 5-doc example. Emits a model (JSON-able dicts) used to render the study doc."""
import math, json, sys

LEVELS = [(1,5),(2,14),(3,20),(4,30),(5,40),(6,40),(7,80),(8,80),(9,120),(9,200)]

def vint(v):
    out = bytearray()
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80); v >>= 7
    out.append(v)
    return bytes(out)

def be32(v): return (v & 0xFFFFFFFF).to_bytes(4, 'big')
def be64(v): return (v & 0xFFFFFFFFFFFFFFFF).to_bytes(8, 'big')
def wstr(b):
    if isinstance(b, str): b = b.encode()
    return vint(len(b)) + b
def shift_pack(v, flag): return (v << 1) | (1 if flag else 0)
def zz64(v):
    v &= 0xFFFFFFFFFFFFFFFF
    sv = v - (1 << 64) if v >> 63 else v
    return ((sv << 1) ^ (sv >> 63)) & 0xFFFFFFFFFFFFFFFF
def hx(b): return ' '.join('%02x' % x for x in b)

# ------------------------------------------------------------------ pools
class Region:
    def __init__(s, start, size, kind, label):
        s.start, s.size, s.kind, s.label = start, size, kind, label

class BytePool:
    def __init__(s):
        s.b = bytearray(); s.wp = 0; s.regions = []; s.log = []
    def ensure(s, n):
        if len(s.b) < n: s.b.extend(b'\0' * (n - len(s.b)))
    def region(s, start, size, kind, label):
        r = Region(start, size, kind, label); s.regions.append(r); return r
    def write_term(s, term, label):
        start = s.wp; s.ensure(start + len(term)); s.b[start:start+len(term)] = term
        s.wp += len(term); s.region(start, len(term), 'term', label); return start
    def alloc_slice(s, label):          # level 0, non-greedy
        sz = LEVELS[0][1]; start = s.wp; s.ensure(start+sz)
        s.b[start:start+sz] = b'\0'*sz; s.wp += sz; s.b[start+sz-1] = LEVELS[0][0]
        s.region(start, sz, 'freq', label); return start
    def alloc_greedy(s, label, level=1):
        sz = LEVELS[level][1]; start = s.wp; s.ensure(start+sz)
        s.b[start:start+sz] = b'\0'*sz; s.wp += sz
        s.b[start] = level; s.b[start+sz-4] = LEVELS[level][0]
        s.region(start, sz, 'prox', label); return start
    def put(s, off, byte, why):
        s.b[off] = byte; s.log.append((off, byte, why))

class SlicedWriter:
    """block_pool_sliced_inserter: write at pos, grow slices lazily"""
    def __init__(s, pool, pos, label):
        s.p, s.pos, s.label = pool, pos, label
    def write(s, byte, why):
        p = s.p
        if p.b[s.pos] != 0:      # hit marker/addr -> alloc next slice
            lvl = p.b[s.pos]; sz = LEVELS[lvl][1]; start = p.wp; p.ensure(start+sz)
            p.b[start:start+sz] = b'\0'*sz; p.wp += sz
            # copy last 3 data bytes of old slice
            p.b[start:start+3] = p.b[s.pos-3:s.pos]
            # overwrite old tail with address
            p.b[s.pos-3:s.pos+1] = be32(start)
            s.pos = start + 3
            p.b[start+sz-1] = LEVELS[lvl][0]
            p.region(start, sz, 'freq', s.label + ' (L%d 扩容)' % lvl)
            p.log.append((start, None, '扩容: 旧 slice 末尾 4B 改写为地址 %d, 旧末 3 数据字节搬到新 slice 开头' % start))
        p.put(s.pos, byte, why); s.pos += 1

class GreedyWriter:
    def __init__(s, pool, slice_off, off):
        s.p, s.slice_off, s.pos = pool, slice_off, slice_off + off
    def write(s, byte, why):
        p = s.p
        assert p.b[s.pos] == 0
        p.put(s.pos, byte, why); s.pos += 1
        assert p.b[s.pos] == 0, 'greedy slice overflow not modelled'
    def cookie(s): return (s.slice_off << 8) | (s.pos - s.slice_off)

def cookie(slice_off, off): return (slice_off << 8) | off
def cstr(c): return 'c(%d,%d)' % (c >> 8, c & 0xFF)

class IntPool:
    def __init__(s): s.v = []

# ------------------------------------------------------------------ field data
class Posting:
    def __init__(s, term, term_off):
        s.term, s.term_off = term, term_off
        s.doc_code = 0; s.int_start = 0; s.doc = 0; s.freq = 0; s.pos = 0; s.size = 1
    def snap(s):
        return dict(doc=s.doc, doc_code=s.doc_code, freq=s.freq, pos=s.pos, size=s.size, int_start=s.int_start)

class FieldData:
    def __init__(s, name, features):
        s.name = name; s.features = features   # 'FREQ|POS' or 'NONE'
        s.terms = {}; s.order = []
        s.last_doc = 0; s.pos_ = 0; s.stats_len = 0
        s.norm_values = {}
    def has_freq(s): return s.features != 'NONE'

class Writer:
    def __init__(s):
        s.bp = BytePool(); s.ip = IntPool(); s.fields = {}; s.field_order = []
    def field(s, name, features):
        if name not in s.fields:
            s.fields[name] = FieldData(name, features); s.field_order.append(name)
        return s.fields[name]

    def emplace(s, f, term):
        t = term.encode()
        if t in f.terms: return f.terms[t], False
        off = s.bp.write_term(t, 'term %s.%s' % (f.name, term))
        p = Posting(t, off); f.terms[t] = p; f.order.append(t); return p, True

    def new_term(s, f, p, did, pos_, tname):
        bp, ip = s.bp, s.ip
        p.int_start = len(ip.v)
        fs = bp.alloc_slice('freq流 %s.%s' % (f.name, tname))
        ps = bp.alloc_greedy('prox流 %s.%s' % (f.name, tname))
        c = cookie(ps, 1)
        ip.v += [fs, fs, c, c, 0]
        p.doc = did
        if not f.has_freq():
            p.doc_code = did
        else:
            p.doc_code = did << 1; p.freq = 1
            w = GreedyWriter(bp, ps, 1)
            w.write(shift_pack(pos_, False), 'prox: %s 在 doc%d 首次出现, 位置 %d (绝对值) → vint(shift_pack(%d,0))' % (tname, did, pos_, pos_))
            ip.v[p.int_start+2] = w.cookie()
            p.pos = pos_
        return p

    def add_term(s, f, p, did, pos_, tname):
        bp, ip = s.bp, s.ip
        if p.doc != did:
            assert f.has_freq()
            dw = SlicedWriter(bp, ip.v[p.int_start], 'freq流 %s.%s' % (f.name, tname))
            prev = p.doc
            if p.freq == 1:
                dw.write(p.doc_code | 1, 'freq流: 刷出 doc%d (freq=1): vwrite64(doc_code|1 = %d)' % (prev, p.doc_code | 1))
            else:
                dw.write(p.doc_code, 'freq流: 刷出 doc%d (freq=%d): vwrite64(doc_code = %d)' % (prev, p.freq, p.doc_code))
                dw.write(p.freq, 'freq流: vwrite32(freq = %d)' % p.freq)
            p.size += 1
            p.doc_code = (did - p.doc) << 1; p.freq = 1; p.doc = did
            endc, startc, last = ip.v[p.int_start+2], ip.v[p.int_start+3], ip.v[p.int_start+4]
            delta = (startc - last) & 0xFFFFFFFFFFFFFFFF
            lo = delta & 0xFF; hi = (delta >> 8) & 0xFFFFFFFF
            dw.write(lo, 'freq流: cookie 增量低字节 (片内偏移差) = %d, 增量=%s - %s' % (lo, cstr(startc), cstr(last) if last else '0'))
            for i, bb in enumerate(vint(hi)):
                dw.write(bb, 'freq流: cookie 增量高部分 vint(slice偏移差=%d)' % hi)
            ip.v[p.int_start+4] = startc
            ip.v[p.int_start+3] = endc
            ip.v[p.int_start] = dw.pos
            w = GreedyWriter(bp, endc >> 8 & 0xFFFFFFFF, endc & 0xFF)
            w.write(shift_pack(pos_, False), 'prox: %s 在 doc%d 首次出现, 位置 %d (绝对值) → vint(shift_pack(%d,0))' % (tname, did, pos_, pos_))
            ip.v[p.int_start+2] = w.cookie()
            p.pos = pos_
        else:
            p.freq += 1
            endc = ip.v[p.int_start+2]
            w = GreedyWriter(bp, endc >> 8 & 0xFFFFFFFF, endc & 0xFF)
            d = pos_ - p.pos
            w.write(shift_pack(d, False), 'prox: %s 在 doc%d 再次出现, 位置 %d, 与上次 %d 之差 %d → vint(shift_pack(%d,0))' % (tname, did, pos_, p.pos, d, d))
            ip.v[p.int_start+2] = w.cookie()
            p.pos = pos_

    def invert(s, f, did, tokens):
        f.pos_ = 0; f.stats_len = 0; f.last_doc = did
        for t in tokens:
            f.pos_ += 1; f.stats_len += 1
            p, new = s.emplace(f, t)
            if new:
                s.bp.log.append((None, None, 'token `%s`@%d: `postings::emplace` 发现新 term → term 字节写入 byte_pool_,走 `new_term_random_access`' % (t, f.pos_)))
                s.new_term(f, p, did, f.pos_, t)
            else:
                same = (p.doc == did)
                s.bp.log.append((None, None, 'token `%s`@%d: 已有 term,%s → `add_term_random_access`' % (t, f.pos_, '同一文档内再次出现(freq++)' if same else '出现在新文档(刷出上一个文档)')))
                s.add_term(f, p, did, f.pos_, t)

# ------------------------------------------------------------------ buffered column
class BufCol:
    def __init__(s, name):
        s.name = name; s.data = bytearray(); s.index = []; s.pending_key = 0; s.pending_off = 0
    def prepare(s, key):
        if s.pending_key != key:
            off = len(s.data)
            if s.pending_key:
                s.index.append((s.pending_key, s.pending_off, off - s.pending_off))
            s.pending_key = key; s.pending_off = off
    def write(s, b): s.data += b
    def snap(s):
        return dict(data=bytes(s.data), index=list(s.index), pk=s.pending_key, po=s.pending_off)

# ------------------------------------------------------------------ example data
DOCS = [  # id, title, body
    ('a1', 'pear',  'red apple red'),
    ('a2', 'fig',   'apple pie'),
    ('a3', 'plum',  'red pie'),
    ('a4', 'apple', 'apple apple'),
    ('a5', 'kiwi',  'pie'),
]

# ------------------------------------------------------------------ run + snapshots
import copy
def snapshot(w, cols):
    return dict(
        b=bytes(w.bp.b), wp=w.bp.wp,
        regions={r.start: (r.size, r.kind, r.label) for r in w.bp.regions},
        ip=list(w.ip.v),
        post={(f.name, t.decode()): p.snap() for f in w.fields.values() for t, p in f.terms.items()},
        cols={k: c.snap() for k, c in cols.items()})

def describe_post(name, term, s, bp_term_off):
    return '%s.%s: {term→@%d, int_start=%d, doc=%d, doc_code=%d, freq=%d, pos=%d, size=%d}' % (
        name, term, bp_term_off, s['int_start'], s['doc'], s['doc_code'], s['freq'], s['pos'], s['size'])

def rg(b, start, size, kind):
    seg = b[start:start+size]
    if kind == 'term': return '"%s"' % seg.decode()
    return hx(seg)

def diff_md(before, after, w, f=None):
    out = []
    # byte pool regions
    rows = []
    for st in sorted(after['regions']):
        size, kind, label = after['regions'][st]
        new = st not in before['regions']
        bb = before['b'][st:st+size] if not new else None
        ab = after['b'][st:st+size]
        if new or bb != ab:
            rows.append((st, size, kind, label,
                         '(未分配)' if new else rg(before['b'], st, size, kind),
                         rg(after['b'], st, size, kind)))
    if rows or before['wp'] != after['wp']:
        out.append('`byte_pool_` 写游标: **%d → %d**' % (before['wp'], after['wp']))
        if rows:
            out.append('')
            out.append('| 池偏移 | 区域 | 变化前 | 变化后 |')
            out.append('|---|---|---|---|')
            for st, size, kind, label, b_, a_ in rows:
                out.append('| %d..%d | %s | `%s` | `%s` |' % (st, st+size-1, label, b_, a_))
    # int pool
    bi, ai = before['ip'], after['ip']
    ch = [(i, bi[i] if i < len(bi) else None, ai[i]) for i in range(len(ai)) if i >= len(bi) or bi[i] != ai[i]]
    if ch:
        out.append('')
        out.append('`int_pool_` 长度 %d → %d,变化项:' % (len(bi), len(ai)))
        out.append('')
        out.append('| 下标 | 含义 | 变化前 | 变化后 |')
        out.append('|---|---|---|---|')
        for i, b_, a_ in ch:
            # find owner
            owner = None
            for (fn, t), s in after['post'].items():
                if s['int_start'] <= i < s['int_start'] + 5:
                    owner = (fn, t, i - s['int_start'])
            names = ['freq 流末尾', 'freq 流起点', 'prox 末尾 cookie', 'prox 起点 cookie', '上个起点 cookie']
            def fmt(v, k):
                if v is None: return '(无)'
                return cstr(v) if k >= 2 and v else str(v)
            out.append('| %d | %s.%s [%d] %s | %s | %s |' % (i, owner[0], owner[1], owner[2], names[owner[2]],
                                                         fmt(b_, owner[2]), fmt(a_, owner[2])))
    # postings
    pc = []
    for k, s in after['post'].items():
        bs = before['post'].get(k)
        if bs != s:
            pc.append((k, bs, s))
    if pc:
        out.append('')
        out.append('`posting` 变化:')
        out.append('')
        for (fn, t), bs, s in pc:
            toff = None
            fd = w.fields[fn]; toff = fd.terms[t.encode()].term_off
            out.append('- 变化前: %s' % ('(不存在)' if bs is None else describe_post(fn, t, bs, toff)))
            out.append('- 变化后: %s' % describe_post(fn, t, s, toff))
    # columns
    for cname in after['cols']:
        bc = before['cols'].get(cname); ac = after['cols'][cname]
        if bc != ac:
            def cs(c):
                if c is None: return '(未创建)'
                return 'data_buf_=[%s] (%dB), index_=%s, pending_key_=%d, pending_offset_=%d' % (
                    hx(c['data']), len(c['data']),
                    '[' + ', '.join('{%d,%d,%d}' % t for t in c['index']) + ']', c['pk'], c['po'])
            out.append('')
            out.append('列 `%s`:' % cname)
            out.append('- 变化前: %s' % cs(bc))
            out.append('- 变化后: %s' % cs(ac))
    return '\n'.join(out)

def run():
    w = Writer()
    cols = {}   # name -> BufCol (cached columns / sort stream)
    steps = []  # (doc, label, log, md_diff)
    # cached column creation order: norm(body) created with body field_data; id created on first STORE
    for di, (idv, title, body) in enumerate(DOCS, 1):
        # --- INDEX title
        for label, fname, feats, text in (('INDEX title', 'title', 'NONE', title), ('INDEX body', 'body', 'FREQ|POS', body)):
            before = snapshot(w, cols)
            w.bp.log = []
            f = w.field(fname, feats)
            if fname == 'body' and 'norm(body)' not in cols:
                cols['norm(body)'] = BufCol('norm(body)')
            tokens = text.split(' ') if fname == 'body' else [text]
            w.invert(f, di, tokens)
            after = snapshot(w, cols)
            steps.append(dict(doc=di, label=label, text=text, log=list(w.bp.log), diff=diff_md(before, after, w), f=fname, tokens=tokens, stats_len=f.stats_len))
        # --- STORE id
        before = snapshot(w, cols)
        if 'id' not in cols: cols['id'] = BufCol('id')
        c = cols['id']; c.prepare(di); val = wstr(idv); c.write(val)
        after = snapshot(w, cols)
        steps.append(dict(doc=di, label='STORE id', text=idv, log=[('id', val, 'write_string(%r) = vint(len)+字节' % idv)], diff=diff_md(before, after, w)))
        # --- STORE_SORTED title
        before = snapshot(w, cols)
        if 'sort(title)' not in cols: cols['sort(title)'] = BufCol('sort(title)')
        c = cols['sort(title)']; c.prepare(di); val = wstr(title); c.write(val)
        after = snapshot(w, cols)
        steps.append(dict(doc=di, label='STORE_SORTED title', text=title, log=[('sort(title)', val, 'write_string(%r)' % title)], diff=diff_md(before, after, w)))
        # --- commit -> finish -> compute_features
        before = snapshot(w, cols)
        c = cols['norm(body)']; c.prepare(di); val = be32(w.fields['body'].stats_len); c.write(val)
        after = snapshot(w, cols)
        steps.append(dict(doc=di, label='commit→Norm2', text='len=%d' % w.fields['body'].stats_len, log=[('norm(body)', val, 'Norm2Writer<uint32_t>::write: write_int(stats.len=%d)' % w.fields['body'].stats_len)], diff=diff_md(before, after, w)))
    return w, cols, steps

if __name__ == '__main__':
    w, cols, steps = run()
    for s in steps:
        print('=== doc%d %s (%s)' % (s['doc'], s['label'], s['text']))
        for l in s['log']:
            print('   log:', l)
        print(s['diff'])
    print('final byte pool wp', w.bp.wp, 'ints', len(w.ip.v))
