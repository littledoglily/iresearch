# 具体场景走查：2个Index线程、3篇文档、1次Commit

配合 [writer_threads_collaboration.md](writer_threads_collaboration.md) 一起看——那份文档讲的是"通用模板"，这份文档用**真实的、可验证的具体数值**（doc_id、tick、segment名字、对象地址）把整条链路走一遍，所有数字都对照源码重新核实过，不是凭印象编的。

---

## 0. 场景设定

```
线程T1：GetBatch() → 插入doc A、doc B → Commit()
线程T2：GetBatch() → 插入doc C          → Commit()
Commit线程：之后触发一次 Commit()，把以上全部内容落盘、生效
```
不涉及删除(Remove)、替换(Replace)、合并(Consolidation)、排序——纯粹的"插入 + 提交"最简路径。

## 1. 初始状态

| 变量 | 初始值 | 依据 |
|---|---|---|
| `tick_`(全局原子计数器) | `1` | `writer_limits::kMinTick(0) + 1`，见`index_writer.hpp:953` |
| `committed_tick_` | `0` | `writer_limits::kMinTick` |
| `segments_active_` | `0` | 还没有任何Transaction在工作 |
| `flush_context_` | 指向 **FlushContext A**(空的：`pending_segments_`/`pending_freelist_`/`segments_`均为空) | |
| `segment_writer_pool_` 内部空闲链表 | 空(从未释放过任何`SegmentContext`) | |
| `committed_reader_` | 空索引(0个segment) | 假设是全新索引 |

---

## 2. 逐步走查

### 步骤① T1: `writer->GetBatch()` → 创建Transaction TX1

`TX1 = {queries_: 0, active_: 空}`

### 步骤② T1: 插入doc "A"

```cpp
auto builder = ctx.Insert();   // → UpdateSegment()
```
- `active_.Segment() == nullptr` → 进入`GetSegmentContext()`：
  - `segments_active_`: `0 → 1`（fetch_add）
  - `GetFlushContext()`：共享锁住A
  - `A.pending_freelist_.pop()` → **空，返回nullptr**(没人释放过任何segment)
  - 走"新建"慢路径：`segment_writer_pool_.emplace(...)` → 内部`free_objects_.pop()`也是空 → 真正`SegmentContext::make(...)`构造一个**全新对象，记作 `SC1`**，绑定一个全新segment名(假设 `NextSegmentId()`返回1 → 名字`"_1"`)
  - 返回 `ActiveSegmentContext{SC1, segments_active_, flush_=nullptr, pending_segment_offset_=kInvalidOffset}`
  - `TX1.active_` = 这个结果
- `writer.initialized()==false` → 跳过flush判断 → `segment.Prepare()`：真正把`SC1`的`segment_writer`绑定到segment名`"_1"`，`initialized_=true`
- 文档"A"落到`SC1`的`segment_writer`里，分配**doc_id=1**(段内本地编号，对应`doc_limits::min()`)

**此刻状态**：`segments_active_=1`，`SC1`持有1篇缓冲文档(A)，`A`的三个容器都还是空的(还没commit，没Emplace)

### 步骤③ T1: 插入doc "B"

- `active_.Segment() != nullptr`(还是SC1)→ **跳过`GetSegmentContext()`整个调用**
- `writer.initialized()==true` → 检查`FlushRequired`：假设远没写满 → 直接返回，继续用`SC1`
- 文档"B"落到`SC1`，分配**doc_id=2**

**关键点**：步骤②③validate了[协作文档1.1节]的结论——同一个Transaction内，`GetSegmentContext()`只在第一次调用，后续`Insert`完全不碰共享状态。

### 步骤④ T1: `TX1.Commit()`

```cpp
bool Commit() noexcept {
  const auto first_tick = writer_->tick_.fetch_add(queries_);  // queries_ = 0 (没有Remove/Replace)
  return CommitImpl(first_tick + queries_);
}
```
> **⚠️ 一个容易忽略的事实**：`tick_.fetch_add(queries_)` 加的是 `queries_`(这个Transaction里Remove/Replace的次数)，**不是插入的文档数**！因为TX1只有Insert、没有Remove/Replace，`queries_`全程是`0`。所以：
> - `first_tick = tick_.fetch_add(0)` → 返回当前值`1`，`tick_`**本身不变**，仍是`1`
> - `CommitImpl(last_tick = 1 + 0 = 1)`

```cpp
segment->Commit(queries_=0, last_tick=1);
```
```cpp
void SegmentContext::Commit(uint64_t queries, uint64_t last_tick) {
  const auto first_tick = last_tick - queries;   // = 1 - 0 = 1
  ... std::for_each(docs.begin()+committed_buffered_docs_, docs.end(), [&](auto& doc){ doc.tick += first_tick; });
  if (first_tick_ == kMaxTick) first_tick_ = first_tick;   // SC1.first_tick_: kMaxTick → 1
  last_tick_ = last_tick;                                    // SC1.last_tick_ = 1
}
```
> **⚠️ 第二个容易忽略的事实**：doc A、doc B 两篇文档的`tick`字段，都被**同一个`first_tick=1`**加过去——也就是说**纯插入(无Remove/Replace)的一批文档，彼此之间共享同一个tick值，不是每篇文档各有一个独立递增的tick**。tick真正要精细区分的是"Remove/Replace操作相对哪些Insert发生的先后"，纯Insert之间不需要互相区分顺序。

然后 `writer_->GetFlushContext()->Emplace(std::move(active_))`：
- `SC1.first_tick_==1`(不是kMaxTick)→ 不走"空事务"分支
- `active.flush_ == nullptr` → `is_null=true`
- 加锁，IIFE：`is_null=true` → **新建**节点：`A.pending_segments_.emplace_back(std::move(active.segment_), offset=0)` → 记作 **`P1`**(包着`SC1`)
- `A.pending_freelist_.push(*P1)`
- `active = {}` → `segments_active_`: `1 → 0`（**注意：这里就减了，不是等SC1对象销毁才减**）

**此刻状态**：
```
A.pending_segments_ = [ P1(offset=0) → SC1(docs=[A,B], first_tick_=1, last_tick_=1) ]
A.pending_freelist_ = [ P1 ]
segments_active_ = 0
tick_ = 1（没变）
```

### 步骤⑤ T2: `writer->GetBatch()` → 创建Transaction TX2，插入doc "C"

```cpp
auto builder = ctx.Insert();   // → UpdateSegment() → GetSegmentContext()
```
- `segments_active_`: `0 → 1`
- `GetFlushContext()`：共享锁住**同一个A**(还没发生过commit，没切代)
- `A.pending_freelist_.pop()` → **成功！弹出`P1`**(刚才T1释放的)
- 返回：
```cpp
return {static_cast<PendingSegmentContext*>(freelist_node)->segment_,   // SC1
        segments_active_, flush.get()/*A*/, freelist_node->value/*=0*/};
```
即 `TX2.active_ = ActiveSegmentContext{SC1, flush_=A, pending_segment_offset_=0}`

★★★ **这是整个例子最关键的一步**：T2拿到的`SC1`，是**T1刚刚用过、已经缓冲了doc A、doc B的同一个`segment_writer`对象**！这就是你之前质疑"为什么要跨线程复用"时，我举的"假设场景"在这里变成了**真实发生的事情**。 ★★★

- `writer.initialized()==true`(SC1从未被Flush/reset过，还是"初始化"状态)，`FlushRequired`仍为false → 直接复用，不调`Prepare()`
- 文档"C"落到**同一个`SC1`**，分配**doc_id=3**

**此刻 `SC1` 内部缓冲着A、B、C三篇文档，物理上最终会落成同一个segment文件。**

### 步骤⑥ T2: `TX2.Commit()`

同样`queries_=0`：
- `first_tick = tick_.fetch_add(0)` → 仍返回`1`，`tick_`仍是`1`
- `segment->Commit(0, last_tick=1)`：
  - `first_tick = 1-0=1`
  - 只更新"自上次commit以来新增"的缓冲文档(即doc C，因为`committed_buffered_docs_`已经记到2了)：`doc_C.tick += 1` → 也是`1`
  - `SC1.first_tick_`已经是1(不是kMaxTick)→ 不变；`SC1.last_tick_` = 1(不变)
- `Emplace(std::move(active_))`：
  - `active.flush_ == A` → `is_null=false`；`flush(A) != this(A)`？否 → 不早退
  - IIFE：`is_null=false` → 走"复用已有节点"分支：`A.pending_segments_[0]`就是`P1` → **还是同一个`P1`，没有新建第二个节点**
  - `A.pending_freelist_.push(*P1)` → P1再次被推回空闲链表
  - `active={}` → `segments_active_: 1→0`

**此刻状态**：
```
A.pending_segments_ = [ P1(offset=0) → SC1(docs=[A,B,C], first_tick_=1, last_tick_=1) ]   ← 还是只有1条记录！
A.pending_freelist_ = [ P1 ]
segments_active_ = 0
```

> 这里也印证了[协作文档1.1节]的另一个结论：`Emplace`会判断"这个segment是不是第一次登记"，**是就新建节点，不是就复用已有节点**——T1第一次是新建，T2第二次是复用，`pending_segments_`自始至终只有1条记录，对应"同一个物理segment"。

---

## 2.1 Tick机制详解——为什么要有tick，怎么分配，commit怎么利用它

本例T1、T2全程没有Remove/Replace，tick的分配过程被简化了（全部共享`tick=1`）。这里单独展开讲清楚tick的**完整工作原理**，并在第7节用一个新场景具体展示"commit无法一次性覆盖segment全部数据"时会发生什么。

### tick要解决的问题

`Remove`/`Replace`的过滤器，不能影响"它执行之后才插入"的文档，也要正确影响"它执行之前已经存在"的文档——包括**同一个Transaction内部**穿插的Insert。要做到这一点，必须有一条**全局、单调递增**的时间线，让任意两个操作（不管是不是同一个线程、同一个Transaction）都能精确比出先后。这条时间线就是`tick`。

### 局部tick的分配——按"目前为止发生过几次Remove/Replace"计数

```cpp
Document Insert(bool disable_flush = false) {
  UpdateSegment(disable_flush);
  return {*active_.Segment(), segment_writer::DocContext{queries_}};   // queries_: Transaction自己的计数器
}

template<bool TickBound = true, typename Filter>
void Remove(Filter&& filter) {
  active_.Segment()->queries_.emplace_back(filter, queries_, QueryContext::kDone);  // 先用当前queries_当这条query的局部tick
  if constexpr (TickBound) { ++queries_; }                                           // 再自增，留给后面的操作用
}
```
**规则：每个操作(无论是Insert还是Remove/Replace)的局部tick，都等于"它发生时，这个Transaction里已经执行过几次Remove/Replace"**。所以：
- 没有任何Remove/Replace发生过时，所有Insert的局部tick都是`0`（本文档①~⑥步骤就是这种情况，所以A、B、C局部tick全是0）
- 一旦插入了一次Remove/Replace，**之后**的Insert局部tick就会变成`1`，再一次Remove/Replace后变成`2`，以此类推

### 全局化——`SegmentContext::Commit`把局部tick"平移"成全局tick

```cpp
void SegmentContext::Commit(uint64_t queries, uint64_t last_tick) {
  const auto first_tick = last_tick - queries;   // 反推这次commit占用的tick区间起点
  auto update_tick = [&](auto& entry) noexcept { entry.tick += first_tick; };   // 所有新增条目统一平移first_tick
  ...
}
```
- `last_tick`来自`Transaction::Commit()`：`tick_.fetch_add(queries_)`先占住全局`tick_`里宽度为`queries_`的一段区间，`first_tick(全局) = fetch_add返回的旧值`，`last_tick = first_tick(全局) + queries_`
- `queries`(参数)恰好等于这次Transaction的Remove/Replace次数，**也恰好是这次占用的tick区间宽度**——这不是巧合，是设计使然：局部tick从`0`数到`queries-1`，区间宽度自然就是`queries`
- `first_tick(局部变量) = last_tick - queries`，精确反推出这段区间的起点，再把**所有**"自上次commit以来新增"的`queries_`/`flushed_docs_`/`docs_context()`条目统一加上这个偏移——局部tick就变成了全局唯一、可跨Transaction比较的真实tick

### `committed_queries_`/`committed_buffered_docs_`/`committed_flushed_docs_`——三条水位线

因为**同一个`SegmentContext`会被多个Transaction先后复用**（就是本文档⑤⑥步骤T1→T2复用`SC1`的情况），每次`Commit()`只能处理"上次commit之后新增的部分"，不能把已经平移过的旧条目重复平移一次——这三个水位线分别记录"`queries_`/`flushed_docs_`/`docs_context()`这三个列表，各自处理到第几条了"，`Commit()`每次只处理`[水位线, 列表当前长度)`这一段，处理完把水位线推到新的长度。

### `SegmentContext::first_tick_`/`last_tick_`——这个segment一辈子的tick范围

跟上面"局部变量"同名但完全不同——这是`SegmentContext`自己的**成员字段**，贯穿它整个生命周期：
- `first_tick_`：初始值`kMaxTick`（哨兵，代表"从没成功commit过"），**只在第一次成功commit时被赋值一次**，之后永远不变——`Emplace`用它判断"这个segment是不是真的产生过有效数据"
- `last_tick_`：**每次commit都可能更新**（只能变大，不能变小），代表"这个segment的数据，目前覆盖到哪个tick为止"——这正是commit线程的`FlushPending`用来判断"这次commit能不能完整覆盖这个segment"的关键依据，第7节会具体展开

---

## 3. Commit线程触发：`writer->Commit()`

### Stage0：切代 + 强制结算

```cpp
auto ctx = SwitchFlushContext();   // 独占锁住A(此刻没人持共享锁，立刻成功) → CAS flush_context_: A→B
ctx->pending_.Wait();              // 没有正在进行中的Emplace/AddToPending，立即通过
const auto flushed_tick = ctx->FlushPending(committed_tick_=0, tick=kMaxTick);
```
`FlushPending`遍历`A.pending_segments_=[P1]`：
- `SC1.first_tick_(1) <= tick(kMaxTick)` → true，处理它
- `flushed_tick = max(0, SC1.last_tick_=1) = 1`
- **`segment->Flush()`** → 这才是`SC1`里3篇文档真正落盘的时刻！内部调`segment_writer::flush()`(就是你之前逐行推演过的那整套`int_pool`/`byte_pool`→`field_writer`→`postings_writer`→磁盘文件+跳表的流程)，产出 `writer_meta_{name="_1", docs_count=3, live_docs_count=3}`，并往`SC1.flushed_`里追加一条记录(覆盖doc索引0~3)
- `tick(kMaxTick) < SC1.last_tick_(1)`？否 → 不需要登记进`next_(B).segments_`(这批数据完全不横跨commit边界，一次性结清)
- `A.segments_.push_back(std::move(segment))` → `A.segments_ = [SC1]`

### Stage1/2：没有旧segment、没有合并结果

`committed_reader_`是空索引(初始commit)，`imports_`是空的 → 这两个Stage什么都不用做，只是`modified = IsInitialCommit(...) = true`。

### Stage3/4：处理`segments_`里的`SC1`，正式生成committed segment

```cpp
for (segment : ctx->segments_) {   // 只有SC1
  for (flushed : segment->flushed_) {   // 只有1条：docs[0..3)
    // flushed_first_tick=1, flushed_last_tick=1
    // tick(kMaxTick) < flushed_first_tick(1)? 否 → 继续处理
    reader = SegmentReaderImpl::Open(dir, flushed.meta, reader_options);   // 打开刚写好的"_1"文件
    // tick(kMaxTick) < flushed_last_tick(1)? 否 → 不缓存进next_cached(这段数据彻底处理完了，不会再跨到下次commit)
    segment_ctx = FlushedSegmentContext{reader, *segment, flushed};
    // 没有删除/替换查询需要应用
  }
}
```
Stage4对这一条`segment_ctx`：`MakeDocumentMask(tick=kMaxTick, ...)` → `flushed_last_tick(1) <= tick(kMaxTick)` → `document_mask`为空、`index=flushed` → 返回`false`(不是"全死"，正常收录)。因为没有新增mask、也不是重复flush，`FlushIndexSegment`把"_1"的最终meta落盘，`readers=[reader("_1")]`，`pending_meta.segments=["_1"的meta]`。

### 收尾：产出PendingContext

```cpp
files_to_sync = GetFilesToSync(...)   // 非空(新segment的文件)
modified = true
InitMeta(pending_meta, flushed_tick=1)   // 因为外部tick是kMaxTick哨兵值，用flushed_tick代替
return PendingContext{ PendingBase{ctx=A, tick=1}, pending_meta, readers=[reader_"_1"], files_to_sync };
```

---

## 4. `ApplyFlush` + `Finish`：真正落盘生效

```cpp
// ApplyFlush
writer_->prepare(dir, index_meta, ..., index_meta_file);   // 写新IndexMeta文件("segments_1"之类)
dir.sync(files_to_sync + [index_meta_file]);                // fsync，数据安全持久化
pending_state_.commit = make_shared<DirectoryReaderImpl>(..., readers=[reader_"_1"]);
pending_state_ = {ctx=A, tick=1, meta, readers, files_to_sync};   // 暂存，还不可见

// Finish
writer_->commit();                                 // 文件级两阶段提交的第二阶段
committed_tick_ = 1;
std::atomic_store(committed_reader_, pending_state_.commit);
// ★★★ 这一刻，doc A、doc B、doc C 才真正对查询可见！★★★
pending_state_.FinishReset();   // 释放A，见下
```

### `FlushContext::Reset()`——A被清空、SC1被彻底回收的完整过程

```cpp
for (segment : segments_ /* =[SC1] */) {
  if (segment.use_count() == 1) segment->Reset();   // 此时SC1被segments_和pending_segments_[0]两处引用，use_count=2，跳过
}
segments_.clear();                                    // segments_这份引用没了，SC1的use_count: 2→1
for (entry : pending_segments_ /* =[P1] */) {
  entry.segment_->Reset();                             // 无条件Reset：清空SC1内部的int_pool/byte_pool(游标归位，内存不释放)
}
ClearPending();   // pending_freelist_清空(排空残留节点) + pending_segments_.clear()(P1这个节点被销毁！)
```
`pending_segments_.clear()`销毁`P1`时，`P1.segment_`(指向`SC1`的最后一个`shared_ptr`)也随之析构——**此时`SC1`的引用计数归零**，触发它的自定义删除器：回到`segment_writer_pool_`自己内部的空闲链表(`free_objects_`)，**而不是`pending_freelist_`**（这是另一层更底层的对象池，专门负责`SegmentContext`对象本身要不要被彻底"退休"复用）。

> 这里揭示了一个之前没讲清楚的细节：实际存在**两层独立的回收机制**——`pending_freelist_`负责"同一个commit周期内，segment在不同Transaction之间快速复用"（本例T1→T2那次复用）；`segment_writer_pool_`的内部空闲链表负责"commit彻底结束后，SegmentContext对象本身要不要被下一次全新的`GetSegmentContext()`调用捞回来复用"（比正式提交之后才会触发）。

---

## 5. 关键状态变化速查表

| 时间点 | `segments_active_` | `tick_` | `A.pending_segments_` | `A.pending_freelist_` | `committed_reader_` |
|---|---|---|---|---|---|
| 初始 | 0 | 1 | [] | [] | 空索引 |
| T1插入A、B后 | 1 | 1 | [] | [] | 空索引 |
| T1 Commit后 | 0 | 1 | `[P1→SC1(A,B)]` | `[P1]` | 空索引 |
| T2插入C后(复用SC1) | 1 | 1 | `[P1→SC1(A,B)]` | `[]`(P1被弹出) | 空索引 |
| T2 Commit后 | 0 | 1 | `[P1→SC1(A,B,C)]` | `[P1]` | 空索引 |
| Commit线程Stage0后 | 0 | 1 | （已转移进`segments_`） | - | 空索引(还没生效) |
| `Finish()`之后 | 0 | 1 | 空(Reset清空) | 空 | **1个segment("_1")，3篇活文档** |

---

## 6. 这个例子验证/澄清了什么

1. **验证**：同一个Transaction内`GetSegmentContext()`只调一次(步骤②③)。
2. **验证并具体化**：不同线程(T1/T2)之间，确实会通过`pending_freelist_`**跨线程复用同一个`SegmentContext`**(步骤⑤)——这正是之前讨论"为什么要跨线程共享池"时的假设场景，这里给出了完全具体、可追踪的真实发生过程。
3. **新发现①**：纯Insert(无Remove/Replace)的批次，批内所有文档**共享同一个tick值**，因为`tick_`的全局递增量是按`queries_`(Remove/Replace次数)算的，不是按文档数。
4. **新发现②**：`segments_active_`的递减发生在`Emplace`里`active={}`的那一刻，**不是等`SegmentContext`对象真正被销毁**——它衡量的是"当前有多少segment正被某个活跃Transaction占用"，不是"系统里存在多少个segment对象"。
5. **新发现③**：存在两层独立的对象回收机制——`pending_freelist_`(commit周期内，同一个FlushContext范围内的快速复用)和`segment_writer_pool_`自己的内部空闲链表(commit彻底结束、`FlushContext::Reset()`清空`pending_segments_`后，才会触发的更底层回收)，两者服务的时间尺度不同。

---

## 7. 延伸场景：当commit无法覆盖segment全部数据时

前6节走查的是"`tick`永远能被完整覆盖"的最简单情况(`CommitInfo::tick`默认就是`kMaxTick`，代表"不设上限，全部覆盖")。这一节接着故事往下走，构造一个**穿插Remove**、并且**commit显式指定了一个较小tick上限**的场景，具体展示`tick < last_tick_`这个分支真正被触发时会发生什么。

### 7.0 承接前文的状态

第一次commit结束后(第4节)：`committed_tick_=1`，`flush_context_`指向B(A已被`Reset()`清空，变回"空闲的另一块缓冲区")，`segment_writer_pool_`内部空闲链表里现在**有了`SC1`这个对象**(因为`FlushContext::Reset()`最后把它释放回了底层池子)。

### 7.1 T3：穿插Insert/Remove/Insert

```
T3: GetBatch() → Insert(doc D) → Remove(filterX) → Insert(doc E) → Commit()
```

**`Insert(doc D)`**：`UpdateSegment` → `GetSegmentContext()`：
- `GetFlushContext()`拿到当前的**B**；`B.pending_freelist_.pop()`→空(B还没人用过)
- 走"新建"路径：`segment_writer_pool_.emplace(...)` → 这次**`free_objects_.pop()`成功了**！捞回了刚才被`Reset()`清空的`SC1`对象——**同一个底层`SegmentContext`对象，经历了完整的"借出→回收→再借出"一轮，现在被绑定到新的segment名`"_2"`**(`NextSegmentId()`返回2)
- `doc D`的局部tick = `queries_(此刻=0)` = `0`，落到`"_2"`，doc_id=1(segment内部重新从1开始编号)

**`Remove(filterX)`**(默认`TickBound=true`)：
```cpp
active_.Segment()->queries_.emplace_back(filterX, queries_=0, kDone);   // filterX局部tick = 0
++queries_;   // TX3.queries_: 0 → 1
```

**`Insert(doc E)`**：`doc E`的局部tick = `queries_(此刻=1)` = `1`——**跟doc D的局部tick(0)不一样了**，因为中间插了一次Remove。doc_id=2。

### 7.2 T3.Commit()——tick_真正开始增长

```cpp
first_tick(全局) = tick_.fetch_add(queries_=1);   // tick_原本是1(T1/T2从没推动过它) → 返回1，tick_变成 1+1=2
CommitImpl(last_tick = 1 + 1 = 2);
segment->Commit(queries=1, last_tick=2);
```
```cpp
first_tick(局部) = last_tick(2) - queries(1) = 1;
// queries_列表：filterX(局部tick=0) → 平移后 tick = 0+1 = 1
// docs_context()：doc D(局部tick=0)→平移后tick=0+1=1；doc E(局部tick=1)→平移后tick=1+1=2
SC1.first_tick_: kMaxTick → 1   // Reset之后first_tick_被清回了kMaxTick，这次重新设置
SC1.last_tick_: 1 → 2            // last_tick_真正增长了！
```
`Emplace`：`SC1`是通过`free_objects_`(底层池子)"全新"拿到的，`active.flush_`是`nullptr` → `is_null=true` → 在`B.pending_segments_`新建节点`P2`(offset=0)，push进`B.pending_freelist_`。

**此刻**：`B.pending_segments_=[P2→SC1(doc D tick=1, doc E tick=2；filterX tick=1；first_tick_=1, last_tick_=2)]`，`tick_=2`。

### 7.3 Commit线程：调用`writer->Commit({.tick = 1})`——显式传一个"覆盖不全"的tick

假设这次调用方**没有用默认的`kMaxTick`**，而是显式要求"只commit到tick=1为止"(真实场景里可能是出于某种精确同步需求)。

**Stage0 `FlushPending(committed_tick_=1, tick=1)`**：
```cpp
for (entry : B.pending_segments_) {   // [P2→SC1]
  first_tick = SC1.first_tick_ = 1;
  last_tick  = SC1.last_tick_  = 2;
  if (first_tick(1) <= tick(1)) {        // 1<=1成立，这个segment至少部分在范围内
    segment->Flush();                     // 把SC1(doc D、doc E)整体物理落盘成segment"_2"——注意：Flush()不管tick，buffer里有什么就全部写！
    if (tick(1) < last_tick(2)) {         // 1 < 2 成立！这次就是"覆盖不全"的情况
      next_segments.push_back(segment);   // 预先登记进 A(=B.next_) 的 segments_，下次commit还要再处理它
    }
    segments_.push_back(std::move(segment));   // 无条件登记进"这一代"(B)的segments_
  }
}
```
**`B.segments_ = [SC1]` 同时 `A.segments_ = [SC1]`**——同一个`shared_ptr`被两代FlushContext共同引用。

**Stage3**(处理`B.segments_`里的`SC1`)：
```cpp
flushed_first_tick = 1(doc D的tick)，flushed_last_tick = 2(doc E的tick)
// tick(1) < flushed_last_tick(2)？是 → 这个FlushedSegment被缓存进 next_cached（供下次commit复用reader，不用重新Open文件）
```

**Stage4 `MakeDocumentMask(tick=1, ...)`**——关键的"逐篇文档判定"：
```cpp
flushed_last_tick(2) <= tick(1)？否 → 走"横跨边界"分支：
document_mask = flushed.document_mask;   // 初始为空
for (rbegin = 最后一篇文档(doc E, tick=2); segment.flushed_docs_[rbegin].tick(2) > tick(1); --rbegin) {
  document_mask.insert(doc E);   // doc E的tick(2) > 1 → 被屏蔽！
  // 下一轮：rbegin指向doc D，tick(1) > tick(1)？否 → 循环停止
}
// 结果：document_mask = {doc E}，doc D 不在mask里
```
**doc D的tick(1)没有超出这次commit的上限(1)，保持可见；doc E的tick(2)超出了，被加进mask、暂时屏蔽。**

### 7.4 这次commit最终产生的效果

```cpp
need_flush = true(因为document_mask非空)
WriteDocumentMask(dir, new_segment.meta, {doc E}, false);   // 写一份mask文件，标记doc E暂时屏蔽
FlushIndexSegment(dir, new_segment);                          // 落盘segment"_2"的最终meta(docs_count=2, live_docs_count=1)
segment_ctx.reader = reader->ReopenDocsMask(dir, new_segment.meta, {doc E});
readers.emplace_back(reader);   // segment"_2"：2篇文档，1篇可见(D)，1篇屏蔽(E)
```
`Finish()`之后：`committed_reader_`里出现segment`"_2"`，**doc D可以被查到，doc E查不到**(即使它已经物理写进了磁盘文件)。`committed_tick_`维持在`1`(因为这次commit只承诺覆盖到tick=1，没有前进)。

### 7.5 doc E什么时候才能变得可见

要等**下一次**commit，用一个`>= 2`的tick(比如默认的`kMaxTick`，或者显式`tick=2`)再处理一遍——这次会轮到`A`变成当前FlushContext(因为上次切代后`flush_context_`指向了`A`)。`A.segments_`里**已经预埋好了`SC1`**(就是7.3里`next_segments.push_back`做的事)，Stage3会**再次**处理它的`flushed_`记录：这次`tick(>=2) < flushed_last_tick(2)`不成立了，说明"不再横跨边界"，`MakeDocumentMask`会发现doc E的tick(2)不再超出上限，不再把它加进mask——doc E终于变得可见。

### 7.6 这个延伸场景说明了什么

1. **`segment->Flush()`是"无条件的、不管tick"**——只要调用了就会把buffer里**当前所有**数据整体物理落盘；真正决定"这批数据里哪些对外可见"的，是**之后**`MakeDocumentMask`按tick逐篇过滤，两者是分开的两步。
2. **同一个segment文件，可能在磁盘上同时存在"已落盘但暂时不可见"的文档**(doc E)——这正是"持久化"和"可见性"分离的具体体现，你之前问过"commit后tick才是真正可以被检索的部分"，这里给出了doc粒度的精确证据。
3. **`next_segments.push_back`让同一个`SegmentContext`可以被两代FlushContext共同引用**，确保"这次没放出来的数据"不会丢失追踪，下次commit能接着处理。
4. **`committed_tick_`不前进，意味着"还欠着账"**——只有当某次commit的tick真正覆盖到某篇文档的tick时，那篇文档才会从mask里被摘除、变得可查询。
