from flush import *

class File:
    def __init__(s, name): s.name = name; s.buf = bytearray(); s.rows = []
    def add(s, step, data, desc, show=None):
        off = len(s.buf); s.buf += data
        s.rows.append(dict(step=step, off=off, size=len(data), hex=hx(data), show=show, desc=desc, fp=len(s.buf)))
        return off


def pack_table(resid, bits):
    """packed::pack for 64 uint64 values: element i occupies bits [i*bits,(i+1)*bits), LSB first,
    words written in host (little-endian) order via write_bytes"""
    vals = list(resid) + [0] * (64 - len(resid))
    words = [0] * bits
    for i, v in enumerate(vals):
        pos = i * bits; w, off = divmod(pos, 64)
        words[w] |= (v << off) & 0xFFFFFFFFFFFFFFFF
        if off + bits > 64: words[w + 1] |= v >> (64 - off)
    return b''.join(x.to_bytes(8, 'little') for x in words)

def packed_note(): return '8B 位打包表(64 项×1bit,仅下标 4 = 1)'


def build(w, cols, M):
    seg = '_0'
    F = {n: File(seg + '.' + n) for n in ('csd', 'csi', 'tm', 'ti', 'doc', 'pos')}
    T = []   # global timeline: (step id, text, [(file, nbytes)])
    def tl(step, text, touched): T.append((step, text, touched))

    # ---------------- T0: .csd header at segment start
    f = F['csd']
    hdr = header('iresearch_11_columnstore_data', 0)
    f.add('S0', hdr, 'header: magic · "iresearch_11_columnstore_data" · ver=0',
          show='magic 3fd76c17 · "iresearch_11_columnstore_data" · ver=0')
    tl('S0', '`segment_writer::reset(meta)` → `col_writer_->prepare`:创建 `.csd` 并写 header(此时还没有任何文档)', [('csd', len(hdr))])

    # ---------------- columns: values in new doc order
    colspec = []   # (id, label, anonymous, name, payload, values[(newdoc, bytes)])
    sv = [(n, v) for n, v, o in M['sorted_vals']]
    nv = [(n, b) for n, b, o in M['colvals']['norm(body)']]
    iv = [(n, b) for n, b, o in M['colvals']['id']]
    norm_payload = bytes([0, 4]) + be32(1) + be32(3)
    colspec = [(0, 'sort(title) 排序列', None, b'', sv), (1, 'norm(body)', None, norm_payload, nv), (2, 'id', 'id', b'', iv)]
    T.append(('F1', '`sort_.stream.Flush`:`push_column`(列 id=0,匿名);判定非有序→排序,得 docmap;按新顺序 `Prepare(new_doc)`+写值(仍在 `column` 内存缓冲)', []))
    T.append(('F2', '`cached_columns_[0]`=norm(body):`push_column`(列 id=1);`FlushDense`(`UseDenseSort(5,5)` 为真)按 docmap 重排写入内存缓冲', []))
    T.append(('F3', '`cached_columns_[1]`=id:`push_column`(列 id=2,列名 "id");同样 `FlushDense`', []))

    # ---------------- T4: commit -> finalize -> csd blocks
    blocks = []
    for cid, label, name, payload, vals in colspec:
        data = b''.join(b for n, b in vals)
        offs = []; o = 0
        for n, b in vals: offs.append(o); o += len(b)
        last_size = len(vals[-1][1])
        base = offs[0]; end = offs[-1]; dist = len(offs) - 1
        avg = int(math.floor((end - base) / dist + 0.5))
        resid = [0]; ab = base
        for x in offs[1:]:
            ab += avg; resid.append(zz64(x - ab))
        all_eq = not any(resid)
        blk = dict(cid=cid, label=label, name=name, payload=payload, vals=vals, data=data, offs=offs, avg=avg, resid=resid, all_eq=all_eq, last_size=last_size)
        f = F['csd']
        blk['addr'] = len(f.buf)
        if not all_eq:
            bits = max(resid).bit_length()
            tab_bytes = (64 * bits + 7) // 8
            f.add('F4', pack_table(resid, bits), '列 %d 块: 偏移表(avg 编码残差 %s,位宽 bits=%d,补零到 64 项,小端字节)' % (cid, resid, bits))
            blk['bits'] = bits
        else:
            blk['bits'] = 0
        blk['data_fp'] = len(f.buf)
        f.add('F4', data, '列 %d 块: 值字节区(%d 个值首尾相接)' % (cid, len(vals)))
        blk['block_data'] = base + blk['data_fp']
        blocks.append(blk)
    f = F['csd']
    f.add('F4', be32(-MAGIC) + be32(0) + b'\0' * 8, 'footer: -magic · 0 · checksum(8B)', show='footer: -magic · alg=0 · checksum')
    tl('F4', '`col_writer_->commit`:3 列各 `flush_block` → `.csd` 追加块;随后写 `.csi`;最后给 `.csd` 写 footer',
       [('csd', sum(r['size'] for r in F['csd'].rows[1:]))])

    # ---------------- .csi
    f = F['csi']
    f.add('F4', header('iresearch_11_columnstore_index', 0), 'header', show='magic 3fd76c17 · "iresearch_11_columnstore_index" · ver=0')
    f.add('F4', vint(3), 'vint(列数=3)')
    for blk in blocks:
        cid = blk['cid']
        parts = []
        parts.append(('string(压缩器名)', wstr('iresearch::compression::none')))
        hdr_show = 'column_header{docs_index=0, id=%d, min=1, docs_count=5, type=%s, props=%s}' % (
            cid, 'kSparse' if not blk['all_eq'] else 'kFixed', 'kNoName' if blk['name'] is None else 'kNormal')
        ty = 0 if not blk['all_eq'] else 2; pr = 2 if blk['name'] is None else 0
        parts.append((hdr_show, be64(0) + be32(cid) + be32(1) + be32(5) + ty.to_bytes(2, 'big') + pr.to_bytes(2, 'big')))
        parts.append(('string(payload)=%s' % (('Norm2Header[%s]' % hx(blk['payload'])) if blk['payload'] else '空'), vint(len(blk['payload'])) + blk['payload']))
        if blk['name']: parts.append(('string(列名)="%s"' % blk['name'], wstr(blk['name'])))
        if not blk['all_eq']:
            parts.append(('块元数据×1: addr=%d, avg=%d, bits=%d, data=%d, last_size=%d' % (blk['addr'], blk['avg'], blk['bits'], blk['block_data'], blk['last_size']),
                          be64(blk['addr']) + be64(blk['avg']) + bytes([blk['bits']]) + be64(blk['block_data']) + be64(blk['last_size'])))
        else:
            parts.append(('long(avg=%d)' % blk['avg'], be64(blk['avg'])))
            parts.append(('块×1: long(data=%d)' % blk['block_data'], be64(blk['block_data'])))
        for d, b in parts:
            f.add('F4', b, '列 %d (%s): %s' % (cid, blk['label'], d))
    f.add('F4', be32(-MAGIC) + be32(0) + b'\0' * 8, 'footer', show='footer: -magic · alg=0 · checksum')
    tl('F4 续', '`.csi`: 列 header + 块元数据 + footer', [('csi', len(f.buf))])

    # ---------------- T5: FlushFields prepare
    f = F['tm']; h = header('block_tree_terms_dict', 2)
    f.add('P0', h, 'header', show='magic 3fd76c17 · "block_tree_terms_dict" · ver=2(IMMUTABLE_FST)')
    f = F['ti']; h = header('block_tree_terms_index', 2)
    f.add('P0', h, 'header', show='magic 3fd76c17 · "block_tree_terms_index" · ver=2')
    f.add('P0', be32(3) + vint(1) + wstr('iresearch::norm2'), 'write_segment_features: int(index_features=FREQ|POS=3) · vlong(特征数=1) · string("iresearch::norm2")',
          )
    f = F['doc']; f.add('P0', header('iresearch_10_postings_documents', 2), 'header', show='magic 3fd76c17 · "iresearch_10_postings_documents" · ver=2(POSITIONS_ZEROBASED)')
    f = F['pos']; f.add('P0', header('iresearch_10_postings_positions', 2), 'header', show='magic 3fd76c17 · "iresearch_10_postings_positions" · ver=2')
    f = F['tm']; f.add('P0', header('iresearch_10_postings_terms', 0) + vint(128), 'postings_writer::prepare 往 .tm 写: header("iresearch_10_postings_terms",0) · vint(skip0 = 块大小 128)',
                       show='header("iresearch_10_postings_terms") · vint(128)=80 01')
    tl('P0', '`field_writer::prepare`:创建 `.tm/.ti/.doc/.pos`(无 PAY/OFFS 故无 `.pay`),各写 header;`.ti` 写段级 features',
       [('tm', len(F['tm'].buf)), ('ti', len(F['ti'].buf)), ('doc', len(F['doc'].buf)), ('pos', len(F['pos'].buf))])

    # ---------------- fields
    fields_meta = {}
    last_meta = None
    for fname in sorted(M['terms']):
        fd = w.fields[fname]; has_freq = fd.has_freq()
        metas = []
        d0, p0 = len(F['doc'].buf), len(F['pos'].buf)
        for t, dec, rem in M['terms'][fname]:
            docs = [(nd, fr, ps) for nd, fr, ps, od in rem]
            fdoc, fpos = F['doc'], F['pos']
            doc_start = len(fdoc.buf); pos_start = len(fpos.buf)
            dcount = len(docs); total_freq = sum(fr for _, fr, _ in docs) if has_freq else 0
            tl_touch = []
            if dcount == 1:
                e_single = docs[0][0] - 1
                fdoc.rows.append(dict(step='%s:%s' % (fname, t), off=doc_start, size=0, hex='', show='(单文档 term: 不写 .doc;文档号存进 term_meta.e_single_doc = %d - 1 = %d)' % (docs[0][0], e_single), desc='', fp=len(fdoc.buf)))
                meta = dict(term=t, docs=dcount, freq=total_freq, doc_start=doc_start, pos_start=pos_start, e_single=e_single)
            else:
                prev = 1; db = bytearray(); pb = bytearray(); dnote = []; pnote = []
                for nd, fr, ps in docs:
                    delta = nd - prev; prev = nd
                    if has_freq:
                        if fr == 1:
                            db += vint(shift_pack(delta, True)); dnote.append('doc%d: Δ=%d,freq=1 → vint(%d)' % (nd, delta, shift_pack(delta, True)))
                        else:
                            db += vint(shift_pack(delta, False)) + vint(fr); dnote.append('doc%d: Δ=%d,freq=%d → vint(%d) vint(%d)' % (nd, delta, fr, shift_pack(delta, False), fr))
                    else:
                        db += vint(delta); dnote.append('doc%d: Δ=%d' % (nd, delta))
                    last = 0
                    for p_ in ps:
                        pb += vint(p_ - last); pnote.append('doc%d 位置%d: Δ=%d' % (nd, p_, p_ - last)); last = p_
                fdoc.add('%s:%s' % (fname, t), bytes(db), 'term "%s" 尾部文档(不足 128): %s' % (t, '; '.join(dnote)))
                if has_freq:
                    fpos.add('%s:%s' % (fname, t), bytes(pb), 'term "%s" 位置增量: %s' % (t, '; '.join(pnote)))
                meta = dict(term=t, docs=dcount, freq=total_freq, doc_start=doc_start, pos_start=pos_start, e_single=None)
            metas.append(meta)
        fields_meta[fname] = metas
        tl('W:%s' % fname, '`field_writer::write(%s)`:逐 term 调 `postings_writer::write`(写 `.doc/.pos`)' % fname,
           [('doc', len(F['doc'].buf) - d0), ('pos', len(F['pos'].buf) - p0)])
        # ---- .tm block
        ftm = F['tm']; block_start = len(ftm.buf)
        suffix = bytearray(); stats = bytearray(); last = dict(doc_start=0, pos_start=0)
        snotes = []
        for m in metas:
            tb = m['term'].encode()
            suffix += vint(len(tb)) + tb
            sb = bytearray(); sb += vint(m['docs'])
            if m['freq']: sb += vint(m['freq'] - m['docs'])
            sb += vint(m['doc_start'] - last['doc_start'])
            if has_freq: sb += vint(m['pos_start'] - last['pos_start'])
            if m['docs'] == 1: sb += vint(m['e_single'])
            stats += sb
            snotes.append('"%s": %s' % (m['term'], hx(sb)))
            last = dict(doc_start=m['doc_start'], pos_start=m['pos_start'])
        blk = vint(shift_pack(len(metas), True)) + vint(shift_pack(len(suffix), True)) + bytes(suffix) + vint(len(stats)) + bytes(stats)
        ftm.add('E:%s' % fname, blk, 'field "%s" 根块(叶子块,%d 个 term):vint(%d<<1|1=最后一块) · vlong(后缀区%dB<<1|leaf) · 后缀区 · vlong(统计区%dB) · 统计区[%s]' % (
            fname, len(metas), len(metas), len(suffix), len(stats), ' | '.join(snotes)))
        # ---- .ti field meta
        fti = F['ti']
        mn, mx = metas[0]['term'], metas[-1]['term']
        sum_d = sum(m['docs'] for m in metas); sum_t = sum(m['freq'] for m in metas)
        meta_b = bytearray(); notes = []
        meta_b += wstr(fname); notes.append('string("%s")' % fname)
        meta_b += be32(3 if has_freq else 0); notes.append('int(index_features=%d)' % (3 if has_freq else 0))
        if has_freq:
            meta_b += vint(1) + vint(0) + vint(1 + 1); notes.append('vlong(特征数=1) vlong(段内特征序号=0) vlong(列id 1 +1 = 2)')
        else:
            meta_b += vint(0); notes.append('vlong(特征数=0)')
        meta_b += vint(len(metas)); notes.append('vlong(term 数=%d)' % len(metas))
        meta_b += vint(5); notes.append('vlong(含该字段的文档数=5)')
        meta_b += vint(sum_d); notes.append('vlong(Σdocs_count=%d)' % sum_d)
        meta_b += wstr(mn) + wstr(mx); notes.append('string(min="%s") string(max="%s")' % (mn, mx))
        if has_freq: meta_b += vint(sum_t); notes.append('vlong(Σfreq=%d)' % sum_t)
        fti.add('E:%s' % fname, bytes(meta_b), 'field "%s" 元信息: %s' % (fname, ' · '.join(notes)))
        fti.rows.append(dict(step='E:%s' % fname, off=len(fti.buf), size=0, hex='', show='FST(前缀 "" → 根块 block_start=%d 于 .tm;immutable_byte_fst::Write,字节数不展开)' % block_start, desc='', fp=len(fti.buf), fst=True))
        tl('E:%s' % fname, '`field_writer::EndField(%s)`:`.tm` 写根块;`.ti` 写字段元信息 + FST' % fname, [('tm', len(blk)), ('ti', len(meta_b))])
        fields_meta[fname + '_block_start'] = block_start
    # ---- end()
    ftm = F['tm']; ftm.add('X', be32(-MAGIC) + be32(0) + b'\0' * 8, 'footer', show='footer: -magic · alg=0 · checksum')
    fti = F['ti']; fti.add('X', be64(2), 'long(fields_count=2)'); fti.add('X', be32(-MAGIC) + be32(0) + b'\0' * 8, 'footer', show='footer')
    for n in ('doc', 'pos'): F[n].add('X', be32(-MAGIC) + be32(0) + b'\0' * 8, 'footer', show='footer')
    tl('X', '`field_writer::end()`:`.tm/.doc/.pos` 写 footer;`.ti` 写 `long(字段数)` + footer', [('tm', 16), ('ti', 24), ('doc', 16), ('pos', 16)])
    return F, T, blocks, fields_meta

def sm_bytes(files):
    b = header('iresearch_10_segment_meta', 1)
    return b

if __name__ == '__main__':
    w, cols, steps = run(); M = flush_model(w, cols)
    F, T, blocks, fm = build(w, cols, M)
    for n, f in F.items():
        print('=====', f.name, len(f.buf))
        for r in f.rows: print('  %4d +%-3d %s | %s' % (r['off'], r['size'], r['show'] or r['hex'], r['desc'][:110]))
    for b in blocks: print({k: v for k, v in b.items() if k in ('cid', 'avg', 'bits', 'addr', 'block_data', 'last_size', 'resid', 'offs')})
    print(fm)
