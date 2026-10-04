# IndexWriter 三类线程协作关系笔记

个人学习笔记，整理自对 `core/index/index_writer.cpp` / `index_writer.hpp` 的逐行阅读，配合 `utils/index-put.cpp` 的实际线程调度代码。核心结论：三类线程通过 **`FlushContext` 双缓冲环** 作为中枢协调点，彼此之间几乎不直接通信，都是"各自往当前生效的 FlushContext 里塞/取数据"。

---

## 0. 三个线程一览

| 线程 | 数量 | 对应代码(`index-put.cpp`) | 触发方式 | 核心职责 |
|---|---|---|---|---|
| **Index 线程**(indexer) | **多个**(`indexer_threads`个) | `indexer_threads` 循环体 | 持续处理输入数据，来一条插一条 | 把文档写进**自己私有**的 `segment_writer`，攒够了自己触发局部 flush |
| **Commit 线程**(committer) | **只有1个** | `commit_interval_ms` 定时循环 | 固定时间间隔 | 把所有 index 线程攒的新数据 + 已完成的合并结果，一次性落盘并让新状态对外可见 |
| **Consolidation 线程** | **多个**(`consolidation_threads`个) | `consolidation_threads` 循环 | 条件变量唤醒(由commit线程通知)或超时 | 后台把多个小 segment 合并成大 segment，结果异步注册等待下次 commit "转正" |

> 日常实际运行时，Index线程和Consolidation线程都是**并发的一批**，只有Commit线程全局唯一——这一点在下面1.的交互图里会体现出来。

---

## 1. 时间线交互图(一个完整周期)

```mermaid
sequenceDiagram
    participant IT1 as Index线程1
    participant IT2 as Index线程2
    participant FC as 当前生效的FlushContext(A)
    participant CT as Commit线程(全局唯一)
    participant CS1 as Consolidation线程1
    participant CS2 as Consolidation线程2
    participant CR as committed_reader_(全局可见状态)

    Note over IT1,IT2: === 多个Index线程并发工作，互不阻塞 ===
    par Index线程1
        IT1->>FC: GetSegmentContext()(领取专属segment_writer)
        IT1->>IT1: 持续Insert(本地写int_pool/byte_pool，线程私有无锁)
        IT1->>FC: Commit() → Emplace(挂进pending_segments_/freelist_)
    and Index线程2
        IT2->>FC: GetSegmentContext()(领取另一个专属segment_writer)
        IT2->>IT2: 持续Insert(本地写int_pool/byte_pool，线程私有无锁)
        IT2->>FC: Commit() → Emplace(挂进pending_segments_/freelist_)
    end
    Note over FC: 两者只在GetSegmentContext/Emplace瞬间靠<br/>共享锁+原子计数器简短同步，插入过程完全独立互不感知

    Note over CS1,CS2: === 多个Consolidation线程并发工作 ===
    par Consolidation线程1
        CS1->>CS1: lock{consolidation_lock_}
        CS1->>CR: policy()选候选(排除consolidating_segments_)
        CS1->>CS1: 候选加进consolidating_segments_，随即解锁
        CS1->>CS1: 真正执行合并(耗时，不持锁)
        CS1->>FC: GetFlushContext()共享锁，结果塞进imports_
    and Consolidation线程2
        CS2->>CS2: lock{consolidation_lock_}(等CS1释放才能拿到)
        CS2->>CR: policy()选候选(此时已自动排除CS1刚选的那批)
        CS2->>CS2: 候选加进consolidating_segments_，随即解锁
        CS2->>CS2: 真正执行合并(耗时，跟CS1并行跑)
        CS2->>FC: GetFlushContext()共享锁，结果塞进imports_
    end
    Note over CS1,CS2: 关键：挑候选靠consolidation_lock_串行化，保证两者不会选中<br/>同一批候选；但真正耗时的合并计算彼此并行、互不等待

    Note over CT: === Commit发生：切代时刻，全局只有这一个CT ===
    CT->>CT: Commit() → 持有 commit_lock_(全局串行，不会有第2个commit同时跑)
    CT->>FC: SwitchFlushContext()：申请独占锁<br/>(阻塞直到IT1/IT2/CS1/CS2所有共享锁持有者都释放)
    CT->>FC: compare_exchange_strong(flush_context_, A, B)<br/>flush_context_ 切到另一代B
    Note over IT1,CS2: 此后所有线程新的 GetSegmentContext()/GetFlushContext()<br/>全部转向B，不再影响正在被处理的A

    CT->>FC: 独占处理A：Stage0~4<br/>(应用删除mask、处理imports_合并结果、<br/>flush新segment、按tick生成mask)
    CT->>CT: ApplyFlush：写新IndexMeta文件+fsync<br/>组装新DirectoryReaderImpl，暂存pending_state_
    CT->>CR: Finish()：writer_->commit() + 原子替换
    Note over CR: ★ 这一刻，新数据才真正对查询可见 ★
    CT->>FC: Reset() + 解锁，A变回空闲，等下次被复用(B.next_=A)
```

**多线程并发的关键点**：
- **多个Index线程之间**：各自持有私有`segment_writer`(对象池领取)，`Insert()`主体无锁；只有"第一次领取"和"commit交还"两个瞬间碰共享的`FlushContext`/`segments_active_`，而且碰的是共享锁+原子操作，不是互斥锁，所以N个index线程基本可以线性扩展。
- **多个Consolidation线程之间**："挑候选segment"这一步靠`consolidation_lock_`互斥锁**严格串行化**(见`index_writer.cpp:1358`的`std::lock_guard lock{consolidation_lock_}`)，确保任意两个consolidation线程不会选中重叠的候选集合；但真正耗时的`MergeWriter`合并计算发生在锁释放**之后**，多个线程可以完全并行执行，不会因为合并耗时而互相阻塞。
- **Commit线程只有一个**：`commit_lock_`保证全局任意时刻只有一次commit在跑，`SwitchFlushContext`的独占锁则保证它接管的那一代FlushContext，此刻不会有任何Index/Consolidation线程还在往里塞数据。

### 1.1 `ActiveSegmentContext` / `PendingSegmentContext` / `FlushContext.segments_` 三者的生产消费关系

这三个名字很像的 `*SegmentContext`，其实是**同一个底层 `SegmentContext`(真正持有 `segment_writer` 的那个对象)在不同生命阶段的三种"包装视角"**，不是三份独立的数据。核心区别：

| 类型 | 本质 | 存放位置 |
|---|---|---|
| `ActiveSegmentContext` | `Transaction` 私有持有的"**借用凭证**"，包了一个 `shared_ptr<SegmentContext>` | `Transaction::active_`（线程私有） |
| `PendingSegmentContext` | 继承自 `Freelist::node_type` 的"**登记节点**"，同样包了同一个 `shared_ptr<SegmentContext>` | `FlushContext::pending_segments_`（deque，地址稳定，所有Index线程共享） |
| `FlushContext::segments_` | 一个 `vector<shared_ptr<SegmentContext>>`，commit真正处理这一代数据时的"**工作集**" | `FlushContext::segments_`（commit线程Stage0现造，Stage3消费） |

#### 时间线图：一个 `SegmentContext` 从"借出"到"落盘"的完整生命周期

```mermaid
sequenceDiagram
    participant TX as Transaction(Index线程)
    participant Pool as segment_writer_pool_ / pending_freelist_
    participant PS as FlushContext.pending_segments_(deque节点)
    participant FS as FlushContext.segments_(Stage0现造的vector)
    participant CT as Commit线程(PrepareFlush)
    participant Disk as 磁盘最终segment文件

    Note over TX,Pool: ① 诞生：ActiveSegmentContext
    TX->>Pool: GetSegmentContext()
    Pool-->>TX: 复用freelist里的SegmentContext，或全新构造一个
    Note over TX: 生产者=IndexWriter::GetSegmentContext()<br/>消费者=这个Transaction自己(全程私有Insert/Remove/Replace)

    Note over TX,PS: ② 转换：ActiveSegmentContext → PendingSegmentContext
    TX->>PS: Commit()/Abort() → Emplace(std::move(active_))
    alt 第一次登记(is_null)
        PS->>PS: pending_segments_.emplace_back(...)新建节点
    else 之前已登记过(AddToPending过)
        PS->>PS: 复用pending_segments_[offset]已有节点
    end
    PS->>Pool: pending_freelist_.push(*node)（挂回空闲链表，标记"可复用"）
    Note over TX,PS: 生产者=Transaction.Commit/Abort调FlushContext::Emplace<br/>消费者有两条路：⑴被另一个GetSegmentContext()弹出复用(回到①循环)<br/>⑵被commit线程的Stage0处理(见下)

    Note over Pool,TX: ⑵a 若被复用：同一个SegmentContext继续给新Transaction用
    Pool-->>TX: 另一个(或同一个)线程的新Transaction GetSegmentContext()弹出这个节点
    Note over TX: 循环回到①，segment_writer里原有数据继续被追加

    Note over CT,FS: ⑵b 若轮到commit：Stage0 FlushPending真正处理
    CT->>PS: 遍历pending_segments_，按tick判断哪些该结算
    CT->>PS: segment->Flush()（强制把还没写满的也落盘一次，产出flushed_记录）
    PS->>FS: 把shared_ptr从pending_segments_条目"移动"进segments_
    opt 这个segment的数据还跨到下次commit(tick < last_tick)
        PS->>FS: 同时也登记进 next_->segments_(下一代也要处理它)
    end
    Note over PS,FS: 生产者=Commit线程自己的FlushPending(Stage0)<br/>消费者=Commit线程自己的PrepareFlush Stage3

    Note over FS,Disk: ③ 终点：Stage3消费segments_，真正落盘成committed segment
    CT->>FS: for segment : ctx->segments_ { for flushed : segment->flushed_ {...} }
    CT->>Disk: 按tick生成mask、落盘，加入readers/pending_meta.segments
    Note over FS,Disk: 生产者=Stage0(segments_本身)+索引期间的segment.Flush()(flushed_列表)<br/>消费者=Stage3/4，最终产出committed_reader_里的正式segment

    Note over FS: ④ 收尾：FlushContext.Reset()
    CT->>FS: segments_.clear()；pending_segments_/pending_freelist_清空(ClearPending)
    CT->>Pool: 对应SegmentContext若use_count()==1(没人再引用)，调用segment->Reset()
```

#### 一句话区分三者

- **`ActiveSegmentContext`**：Index线程"**正在用**"这个segment时的视角——私有、无锁、随Transaction生死。
- **`PendingSegmentContext`**：这个segment"**已交还、等待commit处理**"时的视角——活在 `pending_segments_` 这个共享deque里，同时也是空闲链表的节点，身兼"登记簿"和"复用池"两个角色。
- **`FlushContext::segments_`**：commit线程"**这一轮真正要处理**"的视角——只在Stage0短暂现造出来，专门喂给Stage3遍历处理，处理完连同整个FlushContext一起被清空复用。

三者包的是**同一个** `shared_ptr<SegmentContext>`，只是在"谁能看见它、它处于哪个阶段"这件事上不断变换身份——这也是为什么 `PendingSegmentContext` 要继承 `Freelist::node_type`（你之前研究过的侵入式链表）：省得为"登记"和"复用"这两件事分别造对象。

---

## 2. 各线程自己的处理逻辑

### 2.1 Index 线程

```
GetBatch() → Transaction(私有，move-only)
  循环：
    Insert() / Remove() / Replace()
      → UpdateSegment()
          → 第一次: writer_->GetSegmentContext()（领取专属segment_writer，可能复用空闲池）
          → 写满了: segment.Flush()（局部落盘，不可见）
  Commit() / 析构自动Commit
    → CommitImpl()
        → segment->Commit(queries_, last_tick)
        → writer_->GetFlushContext()->Emplace(active_)（交还给当前代FlushContext）
```

- **只在"第一次拿segment"和"commit交还segment"这两个瞬间**才摸共享状态(`GetSegmentContext`/`GetFlushContext`)，中间大量的 `Insert()` 调用全部发生在**线程私有**的 `segment_writer` 上，无锁、无竞争。

### 2.2 Commit 线程

```
Commit() → std::lock_guard{commit_lock_}（全局串行，同一时刻只有一个commit在跑）
  Start()
    → PrepareFlush()
        Stage0: SwitchFlushContext()（独占锁，等所有共享锁释放后切代）
        Stage1: 遍历已提交旧segment，应用新的删除/替换查询，生成/合并docs_mask
        Stage2: 处理 imports_（后台consolidation结果 + 显式Import），校验候选segment是否还有效
        Stage3/4: 处理这一代里index线程新flush的segment，按tick生成mask，决定要不要落盘
        早退优化: 如果什么都没变(!modified)，直接返回空，零开销
    → ApplyFlush()：写新IndexMeta文件 + fsync所有变更文件 + 组装新reader，暂存pending_state_
  Finish()
    → writer_->commit()（文件级两阶段提交的第二阶段）
    → std::atomic_store(committed_reader_, pending_state_.commit) ★真正生效★
    → pending_state_.FinishReset()（释放这一代FlushContext回池子）
```

### 2.3 Consolidation 线程

```
Consolidate(policy)
  → 读取 *committed_reader_（当前已提交的segment列表）
  → policy(candidates, committed_reader, consolidating_segments_)（排除正在过渡状态的segment）
  → 把入选的候选segment名字加进 consolidating_segments_
  → MergeWriter 真正执行合并（耗时操作，异步跑，不持有任何全局锁）
  → 合并完成后：writer_->GetFlushContext()->imports_.emplace_back(...)
       （结果只是"注册"进当前代，真正生效要等下一次commit的Stage2处理）
```

- 合并本身**不阻塞**任何index线程或commit线程——它只在"读取候选列表"和"注册结果"这两个瞬间短暂接触共享状态。
- 因为合并耗时、是异步的，**合并开始时看到的候选segment，到合并结束时可能已经失效**（被别的commit删空/已被另一次合并吃掉）——这正是Stage2要做"候选校验+重映射"的原因。

---

## 3. 数据结构归属表

### 3.1 Index 线程私有(不共享)

| 数据结构 | 说明 |
|---|---|
| `Transaction` | 每个线程调`GetBatch()`各自拿一份，move-only，不可复制 |
| `ActiveSegmentContext active_` | `Transaction`的成员，持有专属`SegmentContext` |
| `segment_writer`(`postings`/`int_pool`/`byte_pool`/`BufferedColumn`等) | 整条倒排构建链路，线程私有 |
| `QueryContext`（存在`SegmentContext::queries_`里） | 本线程发起的Remove/Replace过滤器 |

### 3.2 Commit 线程私有(单线程串行，无需加锁)

| 数据结构 | 说明 |
|---|---|
| `PrepareFlush`里的局部变量 | `readers`、`pending_meta`、`segment_ctxs`、`partial_sync`等——因为`commit_lock_`保证全局只有一个commit在跑，这些局部容器天然无竞争 |
| `pending_state_` | `IndexWriter`成员，暂存`ApplyFlush`组装好、等`Finish`生效的新状态 |
| `FlushedSegmentContext`/`MakeDocumentMask` | Stage3/4处理新flush segment用的辅助结构 |

### 3.3 Consolidation 线程私有

| 数据结构 | 说明 |
|---|---|
| `MergeWriter` | 真正执行段合并的核心逻辑(对应阅读指南里标记未读的`merge_writer.cpp`) |
| `Consolidation`(候选segment列表) | 本次合并选中的候选集合 |

### 3.4 ★三线程共享/交互用的核心数据结构★

| 数据结构 | 写入方 | 读取方 | 同步机制 |
|---|---|---|---|
| **`flush_context_`**(原子指针) + **`flush_context_pool_{2}`**(双缓冲环) | Commit线程(`SwitchFlushContext`用CAS切换) | Index线程 + Consolidation线程(`GetFlushContext`读取"当前是谁") | 原子CAS + `FlushContext::context_mutex_`(读写锁) |
| **`FlushContext::pending_segments_`**(deque) / **`pending_freelist_`**(无锁栈) | Index线程(`Emplace`/`AddToPending`写入/复用) | Commit线程(Stage3读取处理) | 共享锁(多个index线程) vs 独占锁(commit线程处理时) |
| **`FlushContext::imports_`**(vector) | Consolidation线程(合并完成后写入) | Commit线程(Stage2读取、校验、落地) | 同上，共享锁写入、独占锁处理 |
| **`segments_active_`**(原子计数器) | 所有Index线程 | 所有Index线程(准入控制) | 纯原子fetch_add/fetch_sub，无锁 |
| **`segment_writer_pool_`**(通用对象池，内部也有独立空闲链表) | Index线程 | Index线程 | 池子内部自带无锁栈 |
| **`consolidating_segments_`** | Commit线程(`PrepareFlush`末尾注册"还在过渡状态的segment") | Consolidation线程(选候选时排除这些) | `consolidation_lock_`互斥锁 |
| **`committed_reader_`**(原子shared_ptr) | 仅Commit线程(`Finish`里原子替换) | Consolidation线程(读取当前委交列表)、所有查询请求 | `std::atomic_store_explicit`/`memory_order_release` |
| **`committed_tick_`** | 仅Commit线程 | Commit线程自己(下次PrepareFlush判断tick范围)、间接影响Index线程的tick分配基准 | 无显式锁(由`commit_lock_`串行保护) |

---

## 4. 关键设计洞察(为什么要这么复杂)

1. **Index线程和Commit线程之间，靠"双缓冲FlushContext"解耦**——insert永远只碰"当前代"，commit独占"正被退休的那一代"，两者物理上不会同时碰同一块内存。
2. **Consolidation线程和Commit线程之间，靠"异步注册+下次commit校验"解耦**——合并不持锁、不阻塞，但代价是结果可能"过期"，必须在Stage2重新校验。
3. **真正需要互斥的操作只有两处**：①`SwitchFlushContext`的独占锁(等一瞬间，不是一直锁)；②`commit_lock_`保证全局同一时刻只有一个commit在跑。其余绝大部分操作都是"各自私有数据 + 短暂的原子/共享锁交互"。
4. 整条链路上贯穿的 **`tick`(全局单调递增计数器)**，是协调"谁的变更该不该算进这次commit"的唯一依据，所有segment、query、flushed记录都带着它。

---

## 5. Consolidation 合并的真实数据路径(重要纠正：不经过 int_pool/byte_pool)

最初以为 consolidation 会像 index 线程一样重新构建 `segment_writer`/`int_pool`/`byte_pool`，实际读 `merge_writer.cpp` 后发现**完全不是**——合并时数据早已是"解码好的 postings"，没有重新分词的必要，走的是一条独立的"多 segment 归并迭代器"路径，直接复用正常 flush 末端共用的 `field_writer`/`postings_writer`。

### 5.1 时间线图

```mermaid
sequenceDiagram
    participant Old as 旧Segment A/B/C(已落盘)
    participant Merge as MergeWriter(Consolidation线程内)
    participant FW as field_writer(formats_burst_trie.cpp)
    participant PW as postings_writer(formats_10.cpp)
    participant New as 新Segment文件
    participant FC as 当前FlushContext.imports_
    participant CT as Commit线程(下一次commit)

    Merge->>Old: 直接打开/复用A/B/C的term_reader + doc_iterator
    Merge->>Merge: CompoundFieldIterator 归并多segment的term字典(按term排序k路归并)
    Merge->>Merge: CompoundTermIterator → CompoundDocIterator<br/>+ RemappingDocIterator(把旧doc_id按old2new映射成新doc_id)
    Merge->>FW: field_writer->write(归并后的term_reader, features)
    Note over FW,PW: 完全复用"正常flush"末端同一套编解码，<br/>不经过segment_writer/int_pool/byte_pool
    FW->>PW: pw_->write(*postings, meta)（对每个term）
    PW->>New: 按FormatTraits重新block编码 + 构建skip list，写入新文件
    Merge->>FC: GetFlushContext()共享锁，把结果注册进 imports_
    Note over FC: 新segment此刻只是"候选"，还不在committed_reader_里

    CT->>FC: 下次commit的Stage2：MapCandidates校验A/B/C是否还完整
    alt 候选都还在
        CT->>CT: 标记A/B/C进segment_mask(作废)，新segment并入readers
    else 候选已经失效(被别的commit删空/已被别的合并吃掉)
        CT->>CT: 整个合并结果丢弃(continue)，A/B/C保持原状
    end
```

### 5.2 跟"正常flush路径"的关键区别对比

| 环节 | 正常Index flush路径 | Consolidation合并路径 |
|---|---|---|
| 数据来源 | 原始文本，需要分词 | 已有旧segment的postings，直接读 |
| 中间暂存结构 | `int_pool`/`byte_pool`(varint+差值编码，追加写优化) | **没有**，直接用归并迭代器实时产出 |
| 核心对象 | `field_data`/`postings`/`new_term`/`add_term` | `CompoundFieldIterator`/`CompoundTermIterator`/`CompoundDocIterator`/`RemappingDocIterator` |
| doc_id处理 | 顺序分配新doc_id | 旧doc_id通过`old2new`docmap重映射成新segment里的doc_id |
| 落盘入口 | `fields_data::flush()` → `fw.write(terms, features)` | `MergeWriter::Flush()` → `field_writer->write(field_itr, features)` |
| 最终编码环节 | **相同**：都走 `postings_writer::write()` | **相同**：都走 `postings_writer::write()` |

关键洞察：**两条路径在"写磁盘"这最后一步是完全共用的**(`field_writer`/`postings_writer`)，差异只在"喂给它的 term/doc 迭代器数据是从哪来的"——一个是解码本进程刚积累的内存编码，一个是归并多个已有文件的现成数据。

### 5.3 产出结果的归属

- 新生成的segment文件先注册进当前 `FlushContext::imports_`(写入方是Consolidation线程，读取方是Commit线程)，**此刻还不属于`committed_reader_`**
- 要等**下一次commit**的Stage2校验通过，才会：①把A/B/C标记进`segment_mask`(作废，不进入新的`readers`列表)；②新segment正式并入`pending_meta.segments`/`readers`
- A/B/C的物理文件不是"立刻删除"，而是引用计数归零(没有任何reader再指向它们)后，由后续的`RemoveAllUnreferenced`清理

---

## 6. int_pool/byte_pool → 磁盘文件的完整落地路径(含跳表构建)

这是"正常flush路径"里，你之前逐字节推演过的内存编码，究竟在哪一步被重新解码、重新编码成磁盘格式(含跳表)的完整链路。

### 6.1 时间线图

```mermaid
sequenceDiagram
    participant Pool as int_pool/byte_pool(varint+差值编码)
    participant DI as detail::doc_iterator(field_data.cpp)
    participant TR as term_reader/term_iterator
    participant FW as field_writer::write(formats_burst_trie.cpp)
    participant PW as postings_writer::write(formats_10.cpp)
    participant Skip as SkipWriter(skip_list.cpp)
    participant File as 磁盘最终文件(.doc/.pos/.pay等)

    Note over Pool: 索引阶段持续写入，内容是你深挖过的<br/>shift_pack_64/varint/cookie 编码

    FW->>TR: reader.iterator() 拿到这个字段的term_iterator
    loop 每个term
        FW->>TR: terms->postings(index_features) 拿到doc_iterator
        TR->>DI: 内部就是你研究过的 detail::doc_iterator
        FW->>PW: pw_->write(*postings, meta)
        loop docs.next()（驱动doc_iterator前进）
            PW->>DI: 调用next()
            DI->>Pool: 读取freq_in_/prox_in_(sliced_reader)<br/>shift_unpack_64还原doc差值/freq<br/>vread还原position/offset
            DI-->>PW: 返回解码后的doc_id/freq/position等逻辑值
            PW->>PW: BeginDocument()，按FormatTraits<br/>重新做block位打包编码(标量/SIMD两套)
            alt 凑够一个skip block
                PW->>Skip: skip_.Skip(docs_count, ...)
                Skip->>PW: 回调 WriteSkip(level, out)
                PW->>File: 跳表数据写入(与主数据同一趟遍历，不是单独一遍)
            end
            PW->>File: 主postings数据(doc/freq/pos/pay)持续写入
        end
    end
```

### 6.2 关键代码定位

| 步骤 | 函数 | 文件 |
|---|---|---|
| 触发flush总入口 | `fields_data::flush(field_writer&, flush_state&)` | `core/index/field_data.cpp:1125` |
| 按字段逐个驱动写入 | `fw.write(terms, terms.meta().features)` | `core/index/field_data.cpp:1159` |
| term字典遍历 + 调用postings_writer | `field_writer::write(reader, features)` | `core/formats/formats_burst_trie.cpp:1294` |
| **真正的解码+重编码+跳表构建** | `postings_writer<FormatTraits>::write(doc_iterator&, term_meta&)` | `core/formats/formats_10.cpp:943` |
| 跳表触发点 | `skip_.Skip(docs_count, [this](...){ WriteSkip(level, out); ... })` | `core/formats/formats_10.cpp:988` |

### 6.3 数据结构转换表

| 阶段 | 数据表示形式 | 特点 |
|---|---|---|
| 索引期(`int_pool`/`byte_pool`) | varint变长整数 + 差值编码，分片链式存储(你深挖过的slice/cookie机制) | 为"持续追加写"优化，不利于随机读、不利于批量解压 |
| 解码中间态(`doc_iterator`读出的逻辑值) | 普通的`doc_id_t`/`uint32_t`等逻辑数值 | 纯内存中的临时值，不落盘 |
| 磁盘最终态(`postings_writer`写出的格式) | 按`FormatTraits`做block位打包(比如128篇一组定长位打包) + 独立的跳表结构 | 为"批量快速解压+跳跃查找"优化，牺牲了"追加写"的灵活性换查询性能 |

### 6.4 一句话总结

`doc_iterator`(正常flush场景下，就是`field_data.cpp`里的`detail::doc_iterator`)扮演的是"**解码器**"角色，把索引期专为追加写优化的`int_pool`/`byte_pool`编码解出来；`postings_writer::write()`扮演"**重编码器**"角色，一边消费这些解码出的值，一边按磁盘专用的、面向查询优化的block位打包格式重新写，并在同一趟遍历里顺带把跳表也建好——**索引期编码和磁盘期编码是两套完全不同的格式，`doc_iterator`是连接两者的唯一桥梁**。
