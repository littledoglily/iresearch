from sim import *

MAGIC = 0x3fd76c17
def header(fmt, ver): return be32(MAGIC) + wstr(fmt) + be32(ver)
FOOT = 16

def read_sliced(b, begin, end):
    pos = begin; level = 0; left = min(end - pos, LEVELS[0][1] - 4); out = []
    while pos != end:
        out.append(b[pos]); pos += 1; left -= 1
        if left == 0:
            if pos + 4 >= end: left = end - pos
            else:
                level = LEVELS[level][0]; addr = int.from_bytes(b[pos:pos+4], 'big'); pos = addr
                left = min(end - pos, LEVELS[level][1] - 4)
    return bytes(out)

def greedy_positions(b, cookie, n):
    slice_off, off = cookie >> 8, cookie & 0xFF
    level = b[slice_off]; pos = slice_off + off; left = LEVELS[level][1] - off - 4
    out = []
    while len(out) < n:
        v = b[pos]; pos += 1; left -= 1   # vint assumed single-byte
        assert v & 1 == 0
        out.append(v >> 1)
        if left == 0:
            level = LEVELS[level][0]; addr = int.from_bytes(b[pos:pos+4], 'big'); pos = addr + 1
            left = LEVELS[level][1] - 4 - 1
    return out

def vread(buf, i):
    v = 0; sh = 0
    while True:
        x = buf[i]; i += 1; v |= (x & 0x7F) << sh; sh += 7
        if not x & 0x80: return v, i

def decode_term(w, f, p):
    """returns [(old_doc, freq, [pos...])] using memory only (like detail::doc_iterator)"""
    ip = w.ip.v; b = bytes(w.bp.b)
    fe, fb = ip[p.int_start], ip[p.int_start+1]
    stream = read_sliced(b, fb, fe)
    i = 0; doc = 0; cookie = 0; res = []
    while i < len(stream):
        v, i = vread(stream, i)
        delta, flag = v >> 1, v & 1
        if flag: fr = 1
        else: fr, i = vread(stream, i)
        doc += delta
        lo = stream[i]; i += 1
        hi, i = vread(stream, i)
        cookie += (hi << 8) | lo
        res.append((doc, fr, cookie))
    # pending (last) doc
    res.append((p.doc, p.freq, ip[p.int_start+3]))
    out = []
    for d, fr, ck in res:
        dl = greedy_positions(b, ck, fr) if f.has_freq() else []
        pos = []; acc = 0
        for x in dl: acc += x; pos.append(acc)
        out.append((d, fr, pos))
    return out

def flush_model(w, cols):
    M = {}
    # ---- sorted column -> docmap
    sc = cols['sort(title)']; sc.prepare(0xFFFFFFFF)   # Prepare(eof) closes last pending
    idx = sc.index
    def val(i): 
        k, bg, sz = idx[i]; s = bytes(sc.data[bg:bg+sz]); ln, j = vread(s, 0); return s[j:j+ln].decode()
    # comparer: rhs.compare(lhs) -> descending
    import functools
    def cmp(l, r):
        a, bb = val(l), val(r)
        c = (bb > a) - (bb < a)   # rhs_value.compare(lhs_value)
        if c: return c
        return (l > r) - (l < r)
    order = sorted(range(len(idx)), key=functools.cmp_to_key(cmp))
    docmap = [0] * (len(idx) + 1); new_doc = 1
    sorted_vals = []
    for i in order:
        docmap[idx[i][0]] = new_doc; sorted_vals.append((new_doc, bytes(sc.data[idx[i][1]:idx[i][1]+idx[i][2]]), idx[i][0])); new_doc += 1
    M['docmap'] = docmap; M['order'] = order; M['sorted_vals'] = sorted_vals
    # ---- other cached columns, dense remap
    colvals = {}
    for name in ('norm(body)', 'id'):
        c = cols[name]; c.prepare(0xFFFFFFFF)
        buf = [None] * len(c.index)
        for (k, bg, sz) in c.index:
            nk = docmap[k]; buf[nk-1] = (nk, bytes(c.data[bg:bg+sz]), k)
        colvals[name] = buf
    M['colvals'] = colvals
    # ---- postings decode + remap
    M['terms'] = {}
    for fname in sorted(w.fields):
        f = w.fields[fname]; lst = []
        for t in sorted(f.terms):          # memcmp order
            p = f.terms[t]
            dec = decode_term(w, f, p)
            remapped = sorted([(docmap[d], fr, ps, d) for d, fr, ps in dec])
            lst.append((t.decode(), dec, remapped))
        M['terms'][fname] = lst
    return M

if __name__ == '__main__':
    w, cols, steps = run()
    M = flush_model(w, cols)
    print('docmap', M['docmap'])
    for fn, lst in M['terms'].items():
        for t, dec, rem in lst:
            print(fn, t, 'old:', dec)
            print('      new:', [(d, fr, ps) for d, fr, ps, o in rem])
    print('sorted col', [(n, v, o) for n, v, o in M['sorted_vals']])
    for k, v in M['colvals'].items(): print(k, [(n, hx(b), o) for n, b, o in v])
