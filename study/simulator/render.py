from files import *

def san(s): return s.replace('|', '／')

def render_28(w, cols, steps):
    o = []
    A = o.append
    A('### 2.8 完整例子:5 个文档 × (INDEX + STORE + STORE_SORTED)')
    A('')
    A('> 本节所有字节、偏移都是我按源码逐行写了一个模拟器算出来的(`block_pool` 分级 slice、`new_term_random_access / add_term_random_access`、`BufferedColumn`、`columnstore2`、`burst_trie` 的写入逻辑都按 C++ 复刻),并在模拟里把 flush 阶段从内存池解码回 posting 做了自检。**没有真正运行 iresearch**;个别假设在文中标注。')
    A('')
    A('#### 2.8.1 示例数据与配置')
    A('')
    A('| doc | `id`(STORE) | `title`(INDEX + STORE_SORTED) | `body`(INDEX,FREQ\\|POS,带 Norm2) |')
    A('|---|---|---|---|')
    for i, (a, t, b) in enumerate(DOCS, 1):
        A('| doc%d | `%s` | `%s` | `%s` |' % (i, a, t, b))
    A('')
    A('配置(仿照 `index-put`):')
    A('')
    A('- **format = 1_4**:`columnstore2`、`Norm2`、burst trie `IMMUTABLE_FST`、postings 位置零基(`POSITIONS_ZEROBASED`)。')
    A('- **有 comparator**(`StringComparer`,降序,见 1.4):所以走**随机访问布局**(int_pool 每 term 5 个 int、prox 用 greedy slice + cookie),STORE 列、norm 列全部进 `BufferedColumn`。要演示 STORE_SORTED,必须有 comparator,所以本例只能走这个模式。')
    A('- 三个字段对应的 Field 类型:`id` → `StringField`,只 `Insert<STORE>`;`title` → `StringField`(`string_token_stream`,整串 1 个 term,`index_features=NONE`),`Insert<INDEX>` + `Insert<STORE_SORTED>`;`body` → `TextField`(分词,`FREQ|POS`,features=`Norm2`),只 `Insert<INDEX>`。(数值字段 `NumericField` 的处理方式相同,只是每个值多出 4 个 term,这里省略以免图太大。)')
    A('- 每个文档的插入顺序:`INDEX title` → `INDEX body` → `STORE id` → `STORE_SORTED title` → `commit()`(触发 Norm2)。')
    A('- 假设 `body` 的分析器按空格切词、不转换大小写(示例文本本来就是小写)。')
    A('- `Norm2` 的宽度:`Norm2::MakeWriter({})` 在 `headers` 为空时取 `sizeof(uint32_t)`,所以 norm 值固定 **4 字节大端**([norm.cpp:148-172](core/index/norm.cpp#L148-L172))。')
    A('')
    A('**内存中会出现的对象**(下文 "变化前/后" 表里用到的名字):')
    A('')
    A('| 名字 | 是什么 |')
    A('|---|---|')
    A('| `byte_pool_` | 共享字节池;表里 "池偏移" 是全局偏移。每个新 term 依次分配:term 字节 → freq 流 slice(L0,5B)→ prox 流 greedy slice(L1,14B) |')
    A('| `int_pool_` | 共享 int 池;每个 term 占 5 格:`[0]` freq 流末尾 `[1]` freq 流起点 `[2]` prox 末尾 cookie `[3]` prox 当前文档起点 cookie `[4]` 上一文档起点 cookie |')
    A('| `posting` | `body.red` 这类 "字段.term" 的状态:`doc/doc_code/freq/pos/size/int_start` |')
    A('| 列 `id` | `stored_column("id")` → `cached_column` → `BufferedColumn`(STORE) |')
    A('| 列 `sort(title)` | `sort_.stream`(`BufferedColumn`,STORE_SORTED) |')
    A('| 列 `norm(body)` | body 字段的 `Norm2` feature 列,`cached_column` → `BufferedColumn`(在 `body` 的 `field_data` 构造时创建) |')
    A('')
    A('`cookie c(a,b)` 表示 `(slice 起点偏移 a << 8) | 片内偏移 b`,即 `a*256+b`。greedy slice 的 14 字节布局:`[0]`=level(01) `[1..9]`=数据 `[10]`=下一级哨兵(02) `[11..13]`=补零(将来被改写成地址)。L0 freq slice 的 5 字节:`[0..3]`=数据 `[4]`=哨兵(01)。')
    A('')
    A('**初始状态**:`byte_pool_` 游标=0,`int_pool_` 长度=0,`fields_` 为空,三个 `BufferedColumn` 都还未创建。')
    A('')
    A('#### 2.8.2 逐文档、逐字段的内存变化')
    A('')
    code_path = {
        'INDEX title': '`segment_writer::index` → `fields_.emplace("title")` → `field_data::invert`',
        'INDEX body': '`segment_writer::index` → `fields_.emplace("body")` → `field_data::invert`(每个 token:`pos_ += increment` → `postings::emplace` → `new_term/add_term_random_access`)',
        'STORE id': '`segment_writer::store` → `stream("id")` → `cached_column::Stream()` → `Field::write` → `BufferedColumn::Prepare/write_bytes`',
        'STORE_SORTED title': '`segment_writer::store_sorted` → `sorted_stream(doc)` → `sort_.stream.Prepare(doc)` + `Field::write`',
        'commit→Norm2': '`segment_writer::commit` → `finish()` → `field_data::compute_features` → `Norm2Writer::write(stats, doc, column)`',
    }
    circ = '①②③④⑤'
    order = ['INDEX title', 'INDEX body', 'STORE id', 'STORE_SORTED title', 'commit→Norm2']
    cur = 0
    for s in steps:
        if s['doc'] != cur:
            cur = s['doc']
            A('')
            A('---')
            A('')
            A('##### 文档 doc%d:`id=%s` `title=%s` `body=%s`' % (cur, DOCS[cur-1][0], DOCS[cur-1][1], DOCS[cur-1][2]))
            A('')
            A('`segment_writer::begin(ctx)`:`docs_context_.emplace_back`,`LastDocId()` = **%d**。' % cur)
        A('')
        A('###### doc%d %s %s = `%s`' % (cur, circ[order.index(s['label'])], s['label'], s['text']))
        A('')
        A('路径:' + code_path[s['label']])
        if s['label'] == 'INDEX body':
            A('')
            A('`field_stats`:`len=%d`(本文档 body 的 token 数,稍后写入 norm)。' % s['stats_len'])
        A('')
        for off, byte, why in s['log']:
            if off is None and byte is None:
                A('- ' + why)
            elif isinstance(off, int):
                if byte is None: A('- `byte_pool_` ' + why)
                else: A('- `byte_pool_[%d] ← %02x`:%s' % (off, byte, why))
            else:
                A('- `%s.data_buf_ += %s`:%s' % (off, hx(byte), why))
        A('')
        A(s['diff'])
    A('')
    A('---')
    A('')
    A('#### 2.8.3 doc5 处理完后的完整内存状态(flush 前)')
    A('')
    A('`byte_pool_` 共 **%d** 字节(写游标=%d),按分配顺序的所有区域:' % (len(w.bp.b), w.bp.wp))
    A('')
    A('| 池偏移 | 区域 | 字节 |')
    A('|---|---|---|')
    b = bytes(w.bp.b)
    for r in sorted(w.bp.regions, key=lambda r: r.start):
        A('| %d..%d | %s | `%s` |' % (r.start, r.start + r.size - 1, r.label, rg(b, r.start, r.size, r.kind)))
    A('')
    A('`int_pool_`(40 格,按 term 分组;"cookie" 一列为解码形式):')
    A('')
    A('| term | int_start | `[0]`freq末尾 | `[1]`freq起点 | `[2]`prox末尾cookie | `[3]`prox起点cookie | `[4]`上个起点cookie |')
    A('|---|---|---|---|---|---|---|')
    posts = []
    for fn in w.field_order:
        f = w.fields[fn]
        for t in f.order:
            posts.append((fn, t.decode(), f.terms[t]))
    for fn, t, p in sorted(posts, key=lambda x: x[2].int_start):
        v = w.ip.v[p.int_start:p.int_start+5]
        A('| %s.%s | %d | %d | %d | %s | %s | %s |' % (fn, t, p.int_start, v[0], v[1], cstr(v[2]), cstr(v[3]), cstr(v[4]) if v[4] else '0'))
    A('')
    A('`posting`(每个 term 最后一个文档**还留在 posting 里,没进 freq 流**):')
    A('')
    A('| term | doc | doc_code | freq | pos | size |')
    A('|---|---|---|---|---|---|')
    for fn, t, p in sorted(posts, key=lambda x: x[2].int_start):
        A('| %s.%s | %d | %d | %d | %d | %d |' % (fn, t, p.doc, p.doc_code, p.freq, p.pos, p.size))
    A('')
    A('三个 `BufferedColumn`(最后一个文档仍在 `pending_*`,要到 flush 时 `Prepare(eof)` 才封口):')
    A('')
    A('| 列 | `data_buf_` | `index_` `{key,begin,size}` | pending |')
    A('|---|---|---|---|')
    for name in ('id', 'sort(title)', 'norm(body)'):
        c = cols[name]
        A('| `%s` | `%s` | %s | key=%d, offset=%d |' % (name, hx(c.data), ' '.join('`{%d,%d,%d}`' % t for t in c.index), c.pending_key, c.pending_off))
    return '\n'.join(o) + '\n'

import re
def short(fname, r):
    d = r['desc']; sh = r['show'] or ''
    if sh.startswith('magic') or d == 'header': return 'header'
    if sh.startswith('footer') or d.startswith('footer'): return 'footer'
    m = re.match(r'term "(\w+)" 尾部文档', d)
    if m: return m.group(1) + ' 的文档流'
    m = re.match(r'term "(\w+)" 位置增量', d)
    if m: return m.group(1) + ' 的位置流'
    if sh.startswith('(单文档'): return '(单文档 term,不写)'
    m = re.match(r'field "(\w+)" 根块', d)
    if m: return m.group(1) + ' 字段根块'
    m = re.match(r'field "(\w+)" 元信息', d)
    if m: return m.group(1) + ' 字段元信息'
    if sh.startswith('FST'): return 'FST'
    if 'write_segment_features' in d: return '段级 features'
    if 'postings_writer::prepare' in d: return 'postings 头 + 块大小'
    if d.startswith('long(fields_count'): return '字段数'
    m = re.match(r'列 (\d) 块: 偏移表', d)
    if m: return 'col%s 偏移表' % m.group(1)
    m = re.match(r'列 (\d) 块: 值字节区', d)
    if m: return 'col%s 值区' % m.group(1)
    m = re.match(r'列 (\d) \(.*?\): (.*)', d)
    if m:
        t = m.group(2)
        tag = ('压缩器名' if '压缩器名' in t else 'column_header' if 'column_header' in t else 'payload' if 'payload' in t else
               '列名' if '列名' in t else '块元数据' if '块元数据' in t else 'avg' if t.startswith('long(avg') else 'data(块首地址)' if '块×1' in t else t[:12])
        return 'col%s %s' % (m.group(1), tag)
    if d.startswith('vint(列数'): return '列数'
    return d[:20]

def box(f, width=0):
    lines = ['%s  (共 %d B)' % (f.name, len(f.buf)), '']
    lines.append('%-12s %-6s %s' % ('[起, 止)', '长度', '内容'))
    for r in f.rows:
        if r['size'] > 0:
            lines.append('%-12s %-6s %s' % ('[%d, %d)' % (r['off'], r['off'] + r['size']), '%dB' % r['size'], short(f.name, r)))
        elif r['show'] and r['show'].startswith('FST'):
            lines.append('%-12s %-6s %s' % ('@%d' % r['off'], '?', 'FST(字节数未展开)'))
    return '\n'.join(lines)

def rows_table(f, with_step=True):
    o = ['| 步骤 | 偏移 | 长度 | 内容 | 含义 | 写后文件长度 |', '|---|---|---|---|---|---|']
    for r in f.rows:
        content = r['show'] or ('`%s`' % r['hex'] if r['hex'] else '')
        if r['show'] and not r['show'].startswith(('magic', 'footer', 'header')) and not r['show'].startswith(('(', 'FST', '<')):
            content = '`%s`' % r['show']
        elif r['show']:
            content = r['show']
        o.append('| %s | %d | %d | %s | %s | %d |' % (r['step'], r['off'], r['size'], san(content), san(r['desc']), r['fp']))
    return '\n'.join(o)

def growth(f, hdr_len):
    out = ['%s 累积内容(header 记为 H):' % f.name, '']
    body = []; zero = []
    for r in f.rows:
        if r['desc'] == 'header':
            out.append('%-16s H(%dB)' % (r['step'], r['size'])); continue
        if r['size'] == 0:
            zero.append(r['step']); continue
        if zero:
            out.append('%-16s (无变化: 单文档 term,不写本文件)  [%d B]' % ('%s×%d' % (zero[0].split(':')[0] + ':*', len(zero)), r['off']))
            zero = []
        if r['desc'] == 'footer': body.append('<footer 16B>')
        else: body.append(r['hex'])
        out.append('%-16s H | %s   [%d B]' % (r['step'], ' | '.join(body), r['fp']))
    return '\n'.join(out)

def render_35(w, cols, M, F, T, blocks, fm):
    o = []; A = o.append
    A('### 3.5 例子续:把 2.8 的内存状态 flush 成文件')
    A('')
    A('这一节是**按文件**看 2.8 的 flush 结果(第 3.2、3.3 的例子是**按类型**看同一批数据:内存 → 文件字节的转换)。**每个文件的字节、偏移都由模拟器按源码逻辑算出**;`.ti` 里的 FST 字节我没有展开(`immutable_byte_fst::Write` 的编码比较复杂),只给逻辑内容。文件名前缀假设段名为 `_0`。')
    A('')
    A('#### 3.5.1 flush 时间线:哪一步改了哪个文件')
    A('')
    A('| 步骤 | 发生了什么 | 触及的文件(本步追加字节数) |')
    A('|---|---|---|')
    for step, text, touched in T:
        tt = ', '.join('`.%s` +%d' % (f, n) for f, n in touched) if touched else '(仅内存)'
        A('| %s | %s | %s |' % (step, text, tt))
    A('| (之后) | 回到 `IndexWriter` 提交流程 | `.sm`(见下) |')
    A('')
    A('> 顺序要点:**`.csd` 的 header 在段开始时就写了**;列存 `commit` 先于倒排 flush;`.sm` 在更后面的 commit 阶段才写。')
    A('')
    A('#### 3.5.2 逐文件构造')
    A('')
    names = {'csd': '列存数据', 'csi': '列存索引', 'doc': '文档号/词频', 'pos': '位置', 'tm': 'term 字典块', 'ti': 'term 索引(字段元信息 + FST)'}
    order = ['csd', 'csi', 'doc', 'pos', 'tm', 'ti']
    for n in order:
        f = F[n]
        A('##### `%s`(%s,最终 %d B)' % (f.name, names[n], len(f.buf)))
        A('')
        A('构造过程(每行是一次追加,"写后文件长度" 即该步之后的文件大小):')
        A('')
        A(rows_table(f))
        A('')
        if n in ('doc', 'pos'):
            hl = f.rows[0]['size']
            A('```text')
            A(growth(f, hl))
            A('```')
            A('')
        A('最终布局:')
        A('')
        A('```text')
        A(box(f))
        A('```')
        A('')
        if n == 'csd':
            A('块里的偏移表/块地址如何落进 `.csi`,见下一个文件的 "块元数据"。三列块首尾相接,`block.addr` 指向块起点,`block.data` = 偏移基准 + 值区起点(定长列没有偏移表,所以 `addr == data`)。')
            A('')
            A('偏移表的编码(原理见 3.2.3 的 "均值 + 残差 + zigzag"):以列 0 为例,5 个值的起始偏移 `[0,5,10,15,19]`,`avg = round((19-0)/4) = 5`,残差 = zigzag(实际 − 预测) = `[0,0,0,0,zigzag(-1)=1]`,最大位宽 1,补零到 64 项后打包成 `64×1bit = 8B`,字节为 `10 00 00 00 00 00 00 00`(下标 4 的残差 1 放在第 4 位,按 uint64 小端写出)。列 1(norm,每值 4B)、列 2(id,每值 3B)残差全 0 → `bits = ALL_EQUAL`,偏移表**整个省略**,列被判为定长列(`kFixed`),`.csi` 里只记 `avg` 和 `data`。')
            A('')
        if n == 'csi':
            A('说明:① 列在 `.csi` 里按**列名**排序,匿名列(无名)排在前面,两个匿名列的相对顺序我按 id 顺序示意(小数组上 `std::sort` 走插入排序,实际保持原序,但标准不保证)。② `docs_index=0` 表示该列每个文档都有值,不需要稀疏位图。③ `.csi` 里 `column_header` 的 `type/props` 是 2 字节枚举(`write_short`),表中 `type=0` 为 `kSparse`、`2` 为 `kFixed`,`props=2` 为 `kNoName`。')
            A('')
        if n == 'tm':
            A('说明:只有 term 数 ≤ 25(`min_block_size`),所以每个字段只有**一个根叶子块**(没有子块嵌套)。统计区里 `doc_start/pos_start` 是相对上一个 term 的差分(块首写绝对值 `40`);title 的 5 个 term 都是单文档,`doc_start` 都等于 `.doc` 当前长度 50,所以只有第一个写绝对值,其余差分为 0,文档号放在末尾的 `e_single_doc`(新 doc − 1)。`block_start` 分别是 body 块 %d、title 块 %d,正是 `.ti` 里 FST 指向的位置。' % (fm['body_block_start'], fm['title_block_start']))
            A('')
        if n == 'ti':
            A('说明:`FST` 在 `.ti` 里紧跟每个字段的元信息。本例每个字段只有一个根块,FST 逻辑上就是 `"" → {block_start, 块元信息}` 一条记录,查词时先在 FST 里按前缀定位块,再去 `.tm` 读块。')
            A('')
    # .sm
    A('##### `_0.0.sm`(segment 元数据,commit 阶段写)')
    A('')
    files = ['_0.csd', '_0.csi', '_0.doc', '_0.pos', '_0.ti', '_0.tm']
    sizes = {n: len(F[n].buf) for n in F}
    A('`SegmentMetaWriter::write`([formats_10.cpp:3101](core/formats/formats_10.cpp#L3101))用 **version=1**(`format11` 起,支持主排序标记)。字段取值:')
    A('')
    A('| 序 | 内容 | 值 / 字节 | 来源 |')
    A('|---|---|---|---|')
    rows = [
        ('1', 'header', 'magic · "iresearch_10_segment_meta" · ver=1', '`format_utils::write_header`'),
        ('2', 'string(段名)', '`02 5f 30` = "_0"', '`meta.name`(`file_name(NextSegmentId())` 得 `_N`,这里假设 N=0)'),
        ('3', 'vlong(version)', '`00`', '`meta.version`(初值 0,假设尚未被 `+= 2`)'),
        ('4', 'vlong(live_docs_count)', '`05`', '`meta.live_docs_count` = docs_count − 已删 = 5'),
        ('5', 'vlong(已删文档数)', '`00`', '`docs_count − live_docs_count`;`docs_mask_.count = 0`'),
        ('6', 'vlong(byte_size)', 'Σ 6 个文件字节数(`.ti` 含 FST,未展开)', '`dir_.FlushTracked(meta.byte_size)`;已知部分 %d B(不含 FST)' % sum(sizes.values())),
        ('7', 'byte(flags)', '`03` = HAS_COLUMN_STORE(1) \\| SORTED(2)', '`col_writer_->commit` 返回 true;`meta.sort` 有效'),
        ('8', 'vlong(1 + sort 列 id)', '`01`', '`meta.sort = sort_.id = 0`(排序列是列 id 0)'),
        ('9', 'strings(文件列表)', 'vint(6) + 6 个 string:' + ' '.join('"%s"' % x for x in files), '`meta.files`;来自 flat_hash_set,**顺序不定**,这里按字典序示意'),
        ('10', 'footer', '-magic · 0 · checksum', '`format_utils::write_footer`'),
    ]
    for r in rows: A('| %s | %s | %s | %s |' % r)
    A('')
    A('> `.sm` 不在 `meta.files` 里;本例没有被删文档,我没有核对 `.doc_mask` 在所有分支下的触发条件,按常识理解为不会生成。`segments_N` 则在 `IndexWriter::Commit` 的最后写出,把本 segment 的元数据文件名登记进索引版本,此后读者才能看见这批文档。')
    A('')
    A('#### 3.5.3 一页纸回顾:从 5 个文档到 7 个文件')
    A('')
    A('```mermaid')
    A('flowchart LR')
    A('  subgraph MEM["内存(2.8.3)"]')
    A('    BP["byte_pool_ 211B<br/>term / freq 流 / prox 流"]')
    A('    IP["int_pool_ 40 格<br/>8 个 term × 5"]')
    A('    BC1["BufferedColumn sort(title)"]')
    A('    BC2["BufferedColumn norm(body)"]')
    A('    BC3["BufferedColumn id"]')
    A('  end')
    A('  BC1 -->|"排序→docmap [_,2,4,1,5,3]"| DM(("docmap"))')
    A('  DM --> BC2')
    A('  DM --> BC3')
    A('  DM --> BP')
    A('  BC1 --> CSD[".csd 列0 title"]')
    A('  BC2 --> CSD2[".csd 列1 norm"]')
    A('  BC3 --> CSD3[".csd 列2 id"]')
    A('  CSD --- CSI[".csi 列元数据"]')
    A('  BP --> DOC[".doc / .pos(body 3 个多文档 term)"]')
    A('  IP --> DOC')
    A('  DOC --> TM[".tm 块(body 3 term, title 5 term)"]')
    A('  TM --> TI[".ti 字段元信息 + FST"]')
    A('  CSI --> SM[".sm(commit 阶段)"]')
    A('  TI --> SM')
    A('```')
    return '\n'.join(o) + '\n'
