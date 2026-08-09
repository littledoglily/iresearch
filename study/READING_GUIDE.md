# IResearch 源码阅读路线图

个人学习笔记，不是项目文档，可随时删改。核心方法：**每层先啃 `core/`，再对照 `tests/` 里的同名测试文件找具体例子**，最后拿 `utils/index-put.cpp` 当主线把各层串起来。

用法建议：每看懂一项就把 `[ ]` 改成 `[x]`。✅ 标记的是本轮对话里已经深入聊过的部分。

---

## 第 0 层：`core/utils/` —— 语言工具箱（不依赖任何领域知识，先啃这个）

这一层全是纯 C++ 工程技巧，理解它们不需要先懂"倒排索引"是什么，但后面所有层都在用这些技巧，值得优先攻克。

| 文件 | 关键类型 / 用法 | 需要搞懂什么 | 配套测试 |
|---|---|---|---|
| `core/utils/type_info.hpp`、`type_id.hpp` | `type_info::type_id`（函数指针当类型标签）、`type<T>::id()` | ✅ 为什么用函数指针而不是 RTTI 做类型标识；模板实例化如何产生"每个类型独立的地址" | - |
| `core/utils/attribute_provider.hpp` | `attribute`、`attribute_provider::get_mutable/get`、`irs::get<T>()` | ✅ 类型擦除属性包模式；为什么 `attribute` 是空结构体、非虚 | `tests/utils/attributes_tests.cpp` |
| `core/utils/attribute_helper.hpp` | `attribute_ptr<T>`、`get_mutable_helper<I,Tuple>` | ✅ 编译期展开的 tuple 线性查找；`attribute_ptr<attribute>` 做隐式向上转型的作用 | 同上 |
| `core/utils/memory.hpp` | `memory::Managed`、`managed_ptr`、`OnHeap`/`Tracked` | ✅ 为什么不直接用 `std::shared_ptr`；删除器类型擦除 vs 共享所有权 | - |
| `core/utils/hash_utils.hpp` | `hashed_basic_string_view`、`hash_combine` | ✅ 预缓存哈希值，避免重复计算 | `tests/utils/string_tests.cpp` |
| `core/utils/hash_set_utils.hpp` | `ValueRef<T>`、`ValueRefHash`、`ValueRefEq`、`flat_hash_set<Eq>` | ✅ "轻量句柄 + 缓存哈希"模式；为什么哈希函数和比较函数职责不同 | - |
| `core/utils/block_pool.hpp` | `block_pool`、`block_pool_sliced_inserter/reader`、slice 分级链 | ✅ 为什么不用 `vector<uint8_t>`；slice 链表如何用偏移量当 next 指针 | `tests/utils/block_pool_test.cpp` |
| `core/utils/store_utils.hpp` | `vwrite`/`vread`（变长整数）、`shift_pack_64`/`shift_unpack_64` | ✅ 变长整数编码；用最低位夹带 bool 标记 | `tests/utils/bit_packing_tests.cpp`、`bit_utils_tests.cpp` |
| `core/utils/bit_packing.hpp/.cpp` | `packed::pack_block/unpack_block` | 定长位打包（Frame-of-Reference）算法本身 | `tests/utils/bit_packing_tests.cpp` |
| `core/utils/compression.hpp` + `lz4compression.hpp` | `compressor`/`decompressor`、`compression_registrar` | 压缩算法的注册表模式（和 `formats` registry 同构） | `tests/utils/compression_test.cpp` |
| `core/utils/noncopyable.hpp`、`type_utils.hpp` | 基础 mixin/type traits | 顺手看一眼即可，不必深究 | - |

**读法建议**：每个文件配对应测试一起看,测试里的具体断言比抽象声明更容易建立直觉。

---

## 第 1 层：`core/analysis/` —— 分词/属性机制的实战

体量小,直接建立在第 0 层之上,是检验"属性机制是否真的理解"的试金石。

| 文件 | 关键类型 | 需要搞懂什么 | 配套测试 |
|---|---|---|---|
| `core/analysis/token_stream.hpp` | `token_stream`（继承 `attribute_provider`） | ✅ `next()` 只是推进游标,不返回数据;数据靠属性指针原地更新 | `tests/analysis/token_stream_tests.cpp` |
| `core/analysis/analyzer.hpp` | `analyzer`、`TypedAnalyzer<Impl>`、`empty_analyzer` | ✅ NVI 模式（`get()`非虚 + `get_mutable()`纯虚）;`empty_analyzer` 作为最简单实现范例 | `tests/analysis/analyzer_test.cpp` |
| `core/analysis/token_attributes.hpp` | `term_attribute`/`increment`/`offset`/`payload`/`frequency` | ✅ 这些具体属性都是空虚函数表的小 POD | `tests/analysis/token_attributes_test.cpp` |
| `core/analysis/token_streams.hpp` | `basic_token_stream`、`string_token_stream`、`boolean_token_stream` | ✅ `std::tuple<...> attrs_` + `irs::get_mutable(attrs_, id)` 的具体落地 | - |
| `core/analysis/ngram_token_stream.cpp`（当前打开的文件） | `ngram_token_stream_base` | [ ] 具体的、真正做"分词"逻辑的实现长什么样（对比 `string_token_stream` 的"不分词") | `tests/analysis/ngram_token_stream_test.cpp` |
| 任选 1-2 个其他具体分析器 | `segmentation_token_stream`、`delimited_token_stream` 等 | [ ] 巩固"同一接口、不同实现"的感觉 | `tests/analysis/segmentation_stream_tests.cpp` 等 |

---

## 第 2 层：`core/index/` 写入路径 —— 本次对话主线，已覆盖最深

**读法建议**：不要孤立读文件,拿 `utils/index-put.cpp` 的 `put()` 函数当剧本,从 `builder.Insert<Action::INDEX>(field)` 一路点进去,遇到不懂的再钻进对应实现。

| 文件 | 关键类型 | 需要搞懂什么 | 配套测试 |
|---|---|---|---|
| `core/index/postings.hpp/.cpp` | `posting`、`postings`（set+vector 组合） | ✅ 为什么词典是"set存下标+vector存数据"而不是 `hash_map<term,posting>` | `tests/index/postings_tests.cpp` |
| `core/index/field_data.hpp/.cpp` | `field_data`、`fields_data`、`invert()`、`new_term/add_term` | ✅ 倒排构建的核心算法：doc_code 差值编码、prox 流位置编码、"最后一篇文档留在结构体里不落盘"的读写协作 | `tests/index/field_meta_test.cpp`（附近）、`postings_tests.cpp` |
| `core/index/segment_writer.hpp/.cpp` | `segment_writer`、`stored_column`、`sorted_column`、`flush()` | ✅ 一次段写事务的调度者;`index()`/`store()`/`flush()` 三段式;`IsSubsetOf` 一致性校验 | `tests/index/segment_writer_tests.cpp` |
| `core/index/index_writer.cpp` | `IndexWriter`、`SegmentContext::Flush()`、`PrepareFlush()` | ✅ segment 何时落盘;两阶段提交（`pending_segments_N`→`segments_N`) | `tests/index/index_tests.cpp`（端到端,体量大,适合验证整体理解） |
| `core/utils/index_utils.cpp` | `FlushIndexSegment()`、`ConsolidateTier` | ✅ segment_meta 落盘;consolidation 策略 | `tests/index/consolidation_policy_tests.cpp` |
| `core/index/merge_writer.cpp` | `IsSubsetOf(feature_map_t)` 等 | [ ] 段合并（consolidate）时倒排数据怎么归并 | `tests/index/merge_writer_tests.cpp` |
| `core/index/iterators.hpp` | `doc_iterator`、`term_iterator` 抽象接口 | [ ] 读取端的统一迭代器接口,是 formats 层和 search 层的粘合剂 | - |

---

## 第 3 层：`core/formats/` —— 磁盘格式（最复杂，务必等第 2 层扎实后再碰）

这一层本质是"把第 2 层内存态倒排重新编码成磁盘格式",没有第 2 层的直觉会很难懂。位运算/SIMD 部分建议**用调试器单步跑小测试**,不要只靠眼睛读。

| 文件 | 关键类型 | 需要搞懂什么 | 配套测试 |
|---|---|---|---|
| `core/formats/formats.hpp/.cpp` | `format`、`formats` 注册表、`format_registrar` | ✅ 插件注册表模式;`format` 作为抽象工厂,一次产出配套读写器全家桶 | `tests/formats/formats_tests.cpp` |
| `core/formats/formats_10.cpp` | `postings_writer<FormatTraits,Wand>`、`format_traits`/`format_traits_sse4` | [ ] 128篇一个block；delta+定长位打包；标量 vs SIMD 两套 `FormatTraits` | `tests/formats/formats_10_tests.cpp`（先看这个,再看 `formats_12/13/14/15`) |
| `core/formats/formats_burst_trie.cpp` | `burst_trie::field_writer/field_reader` | [ ] block-tree 词典 + FST 索引结构（类似 Lucene BlockTreeTermsWriter） | 含在 `formats_tests.cpp` 里 |
| `core/formats/skip_list.hpp` | `SkipWriter` | [ ] 跳表如何加速大 posting list 的定位 | `tests/formats/skip_list_test.cpp` |
| `core/formats/columnstore.cpp` | `columnstore::writer`（v1,LZ4） | ✅ 旧版列存,`write_compact()` 压缩比判定回退逻辑 | 含在 formats 测试里 |
| `core/formats/columnstore2.hpp/.cpp` | `columnstore2::writer`、`sparse_bitmap_writer` | ✅ 新版列存,稀疏位图 + 差值打包;⚠️ 目前硬编码忽略压缩配置 | `tests/formats/columnstore2_test.cpp`、`sparse_bitmap_test.cpp` |
| `core/utils/compression.hpp` | （见第0层） | 压缩策略如何被 columnstore 调用 | - |

---

## 第 4 层：`core/search/` —— 查询执行（依赖前面所有层，放最后）

| 文件 | 关键类型 | 需要搞懂什么 | 配套测试 |
|---|---|---|---|
| `core/search/filter.hpp` | `filter`、`prepared` | [ ] 查询树构建（`by_term`/`by_range`...）与"准备好的可执行过滤器"的分离 | `tests/search/term_filter_tests.cpp` 等 |
| `core/search/scorer.hpp`、`bm25.hpp`、`tfidf.hpp` | `Scorer`、`FieldCollector`/`TermCollector` | [ ] 打分插件机制;BM25/TFIDF 具体怎么用 field_stats | `tests/search/bm25_test.cpp`、`tfidf_test.cpp`、`scorers_tests.cpp` |
| `core/index/index_reader.hpp` | `IndexReader`、`SubReader`（均为 `shared_ptr`） | [ ] 为什么这里是真正需要共享所有权的场景（对比第0层 `memory::Managed` 的讨论） | `tests/search/index_reader_test.cpp` |
| `core/search/boolean_filter.hpp` 等 | `And`/`Or`/`Not` | [ ] 布尔组合查询的迭代器组合方式 | `tests/search/boolean_filter_tests.cpp` |

---

## 通用建议

1. **善用调试器**：`block_pool` 切片链、`formats_10.cpp` 位打包这类代码,单步跑一个小测试用例观察变量变化,比反复读源码快得多。
2. **`utils/index-put.cpp` / `utils/index-search.cpp` 是最好的主线剧本**：写入看 `index-put.cpp`,查询看 `index-search.cpp`,遇到不懂的类型再钻进 `core/` 对应实现,比按目录顺序通读效率高。
3. **测试文件命名规律**：`core/xxx/yyy.hpp` 基本对应 `tests/xxx/yyy_test(s).cpp`,遇到看不懂的类,先搜同名测试文件。
4. **不要跳级**：第 3 层（磁盘格式）建立在第 2 层（内存态倒排）的直觉之上,没吃透第 2 层就啃 `formats_10.cpp` 容易受挫。
