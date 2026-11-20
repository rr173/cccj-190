# spatial-catalog — 离线区域边界/线设施磁盘空间目录

为离线采集的区域边界与线设施提供**静态构建、只读查询**的磁盘空间索引。
输入是无序、可大于内存的 JSONL（每行一个 `object_id` + GeoJSON 几何），
构建出自研的持久化 Hilbert 装箱 R 树；查询支持闭矩形 window 与精确 kNN
nearest。包围盒只用于剪枝，**业务答案永远由几何真值决定**。

在此之上，本项目还提供**“线设施—区域边界长度归属交叉表”**（cross-table）：
对两份已构建目录逐线 × 逐区域求精确相交长度，产出可检查、版本化、确定性
排序的交叉表目录，而不是又一种只返回候选 ID 的空间查询。

## 安装与运行

依赖：Python ≥ 3.9 + Shapely（1.8 或 2.x，仅用于单件几何的合法性判断、
`intersects`、点到几何精确距离与单件 `intersection().length`；**不**使用
STRtree、空间数据库或任何现成索引建树）。

```bash
pip install shapely pytest
cd spatial_catalog            # 仓库根目录

# 构建（输入无需有序，可大于内存；--cache-bytes 是内存预算）
python3 -m spatial_catalog build --input data.jsonl --out cat \
    --cache-bytes 8388608 --page-size 4096 --max-payload-bytes 1048576

# 闭矩形查询（边界相触算相交；输出 id 与真实遍历统计）
python3 -m spatial_catalog window --catalog cat --rect 0 0 100 100

# k 近邻（真实几何距离升序，等距按 object_id Unicode 码点序）
python3 -m spatial_catalog nearest --catalog cat --x 10.5 --y 20.5 --k 10

# 目录信息 / 完整校验（全文件哈希 + 每页/每记录 CRC + 引用合法性）
python3 -m spatial_catalog info --catalog cat
python3 -m spatial_catalog validate --catalog cat

# 诊断：运行时生成数据，对照全扫报告节点/载荷访问量与 RSS
python3 -m spatial_catalog diag --out /tmp/diag --n 100000

# ===== 线—区域长度归属交叉表 =====
# 线目录几何必须是 LineString/MultiLineString；区域目录必须是
# Polygon/MultiPolygon。原目录以只读方式打开，绝不被修改。
python3 -m spatial_catalog xtab build \
    --lines line.cat --zones zone.cat --out xt \
    --cache-bytes 8388608 --max-spill-bytes 1073741824 --force

python3 -m spatial_catalog xtab validate --crosstab xt
python3 -m spatial_catalog xtab info     --crosstab xt
python3 -m spatial_catalog xtab diag --out /tmp/xtab-diag \
    --n-lines 4000 --n-zones 1000

# ===== 线设施节点—边拓扑网络 =====
# 从一份已构建目录抽取可直接检查连接关系的网络（不是邻近/框选查询，也不是
# 区域交叠报表）。原目录只读打开，绝不被修改。
python3 -m spatial_catalog topo build \
    --catalog cat --out topo \
    --cache-bytes 8388608 --max-spill-bytes 1073741824 --force
# 仅处理选定对象（可选：--ids-file 名单、--id-prefix 前缀，可重复）
python3 -m spatial_catalog topo build --catalog cat --out topo-sel \
    --ids-file selected.txt --id-prefix net- \
    --exclude-non-lines --force
python3 -m spatial_catalog topo validate --topology topo
python3 -m spatial_catalog topo info     --topology topo
python3 -m spatial_catalog topo diag     --out /tmp/topo-diag \
    --n 6000 --big 1500      # diagnose 是 diag 的别名

# ===== 巡检覆盖对账（inspcov）=====
# 给定拓扑边名册、桩号登记表与若干无序巡检 JSONL，重现两个收件截止号
# 上的覆盖台账并逐区段对账。原拓扑只读打开，绝不被修改。
python3 -m spatial_catalog inspcov build \
    --topology topo --registry registry.jsonl \
    --inspections crew-a.jsonl crew-b.jsonl \
    --date-from 2026-11-01 --date-to 2026-12-01 \
    --cutoff-before 12 --cutoff-after 18 --out inspcov \
    --cache-bytes 8388608 --max-spill-bytes 1073741824 --force
python3 -m spatial_catalog inspcov validate --coverage inspcov
python3 -m spatial_catalog inspcov info     --coverage inspcov
python3 -m spatial_catalog inspcov diag     --out /tmp/insp-diag \
    --n-edges 400 --trunk-obs 4000

# 测试
python3 -m pytest
```


## 数据契约与拒收

输入 JSONL，每行：`{"object_id": <非空字符串>, "geometry": <GeoJSON 几何>}`。
几何类型仅 `Point/LineString/Polygon` 及对应 `Multi*`，二维笛卡尔坐标。
以下情况**逐条拒收**并给出机器可读证据（`<out>.failed/rejects.jsonl`，
含行号、id、原因码），且整个构建失败、目录不可查询（无 manifest）：

- JSON 无法解析、含 `NaN/Infinity` 字面量、坐标非有限或超出 `|v| ≤ 1e15`
  （`--max-coord` 可调）；
- 空几何（如 `coordinates: []`）、非二维坐标、未闭合环、自相交等非法面
  （Shapely `is_valid` 为假）——**只拒收，不修复**；
- `object_id` 缺失/非字符串/为空/超过 4096 字节，或**重复 id**（外排后
  相邻检测，证据含所有重复行号）；
- 单件 WKB 超过 `--max-payload-bytes`（默认 **1 MiB**，几何尺寸的明确上限）；
- 不支持的类型（含 `GeometryCollection`）。

构建在 `<out>.build-<pid>/` 中进行，全部文件写完、fsync 后才**原子写入
`manifest.json`（提交点）**，再 rename 到位。任何失败/中断的目录都没有
manifest，`Catalog()` 打开即 `NotACatalogError`。

## 磁盘布局（版本化、定长界、可检错）

```
cat/
  manifest.json   # 提交点：版本、对象数、深度、根页、两文件长度+SHA-256
  nodes.bin       # 头(magic,版本,页大小,页数,深度,根页,条目数,CRC) + 定长页
  payloads.bin    # 头(magic,版本,记录数,文件长,CRC) + 变长记录(id + WKB)
```

- 每个节点页：`CRC32 + kind + count + 条目`，叶条目 = 后代包围盒 +
  载荷(偏移,长度)，内部条目 = 子树包围盒 + 子页号。**几何载荷与树节点
  分离**，查询只在叶条目通过包围盒裁剪后才解码载荷。
- 每个载荷记录带 CRC32；manifest 钉住两文件精确长度（打开即核对）与
  SHA-256（`validate` 全量核对）。截断、位翻转、越界引用分别抛
  `CorruptionError` / `IllegalReferenceError`，绝不静默漏对象。

## 索引结构（自研，非库代建）

1. 流式校验 + 写载荷，条目（id、行号、bbox、载荷引用）落临时文件；
2. 外排按 id 查重（分块排序 + 多路归并，块大小受 `--cache-bytes` 约束）；
3. 外排按 bbox 中心的 **Hilbert 曲线键**（31 位/轴，62 位键）排序；
4. 按序装箱叶页，父级包围盒逐层向上装箱（每层流式读写临时文件，
   内存占用与数据量无关）。

## 查询语义

**window(minx, miny, maxx, maxy)**：闭矩形。返回与矩形**真实相交**
（Shapely `intersects`，边界相触为真）的 id；退化矩形（零宽/零面积）
按线段/点处理。遍历为深度优先，栈深 ≤ 树深 × 页容量，不随数据量增长。
id 顺序为遍历序（确定但不排序）。

**nearest(x, y, k)**：按**真实几何距离**升序、等距按 `object_id`
Unicode 码点序，至多 k 件。`k=0` 返回空；`k > 总数` 返回全部。
最佳优先搜索：优先队列按"点到条目 bbox 的距离下界"弹出，内部条目
展开、叶条目解码载荷算精确距离；**仅当队列最小下界严格大于当前第 k
名的真实距离时才停止**——下界相等者仍会被处理，因此等距 id 决胜是
精确的。不会按 bbox 距离取 k 再补算，也不依赖装箱相邻性。

### 数值距离比较约定

- 距离为 Shapely/GEOS 返回的 float64，排序键是 `(距离 float64, id)`；
  我们**不**把浮点近似相等声称为数学等距——验收用例的等距（如到原点
  距离恰为 1 的四个整坐标点）在二进制浮点下精确可表示。
- bbox 距离下界用 `math.nextafter(·, -inf)` 向下让 1 ulp，保证浮点舍入
  不会使下界超过真实距离（宁可多访一页，绝不误剪）。

### 每次查询的真实统计

`nodes_visited`（读页数）、`payloads_decoded`（解码载荷数）、
`exact_predicates`（精确谓词/距离次数）、`pq_spills`/`pq_high_water`
（候选队列溢写与峰值）。测试用它们验证确实走了索引而非全扫。

## 内存预算（构建与查询分别接受）

| 预算 | 作用域 |
|---|---|
| build `--cache-bytes` | 外排分块（≈预算一半/块）、写缓冲；溢出即落盘归并 |
| query `--page-cache-bytes` | 节点页 LRU（≥4 页） |
| query `--queue-spill-bytes` | nearest 候选优先队列在内存的条目上限，超出**溢写磁盘**（有序运行文件 + 归并），高重叠最坏情况也不会无限驻留 |

载荷不缓存（按引用即读即弃）。预算之外的总驻留：window 栈
（≤ 深度×页容量）、Top-k 堆（≤ k）。

## 已知边界

- 静态目录：构建后只读，更新需重建。
- 全重叠最坏情况下剪枝失效，nearest/window 退化为近全扫——诊断与测试
  如实报告（`diag` 的 overlap 段、`test_scale.py`），不粉饰。
- 空输入合法：得到空目录，查询返回空。

## 复现

```bash
python3 -m pytest                 # 全部单测/差分/规模/损坏/CLI 测试
python3 -m pytest tests/test_scale.py -s   # 查看规模诊断输出
python3 -m spatial_catalog diag --out /tmp/diag --n 100000
```

实测诊断数据（10 万稀疏 + 2 万全重叠，含节点/载荷访问量对照全扫、
RSS）见 [DIAGNOSTICS.md](DIAGNOSTICS.md)。

差分测试用独立顺序全扫（`spatial_catalog/brute.py`，只用 Shapely 单件
谓词、不触碰生产树/剪枝代码）对照随机查询；所有规模数据运行时由
`spatial_catalog/datagen.py` 以固定种子生成。

---

# 线设施—区域边界长度归属交叉表

`window`/`nearest` 即使完全按合同工作，也只能回答“哪些 id 命中”，无法回答
“每条线分别落入哪些区域、各自相交长度是多少”。交叉表把空间关系固化成
**逐线—逐区域、可检查、可复现**的分析输入。

## 输出目录（版本化、原子提交）

```
xt/
  xtab_manifest.json  # 提交点：格式版本、两份输入目录的内容身份、
                      #   记录数、rows.tsv 长度 + SHA-256（最后原子写入）
  rows.tsv            # 确定性 UTF-8 行文件（两行固定头 + 数据行）
  build_report.json   # 构建诊断：全部为实测计数（无硬编码/估算）
```

数据行（无引号、无转义；id 含 Tab/CR/LF 在构建期即被拒）：

```
source_id ⇥ zone_id ⇥ intersect_length ⇥ source_length ⇥ ratio ⏎
```

* 仅当线与区域的交集具有**严格为正的一维长度**时才输出一行；点接触、
  沿边界相触但交集长度为 0 **不输出配对行**（计入 `zero_length_pairs`）。
  沿边重合但长度为正的交集**会**被归属（这是真实的一维长度）。
* `intersect_length`：交集线几何在原坐标单位下的 GEOS 平面长度；
  `source_length`：输入线几何的 GEOS 长度；`ratio = intersect_length /
  source_length`（float64 商，校验时逐位核对）。
* `GeometryCollection` 等集合按 GEOS 对集合的长度语义处理（各部件长度
  之和，点部件贡献 0）。
* 区域之间允许重叠：每个 `(线, 区域)` 配对独立统计，跨区域长度之和
  **不承诺守恒**，重叠段不会被分摊给某个区域，而是在每个命中区域各计一次。
* 一个 `(source_id, zone_id)` 至多一行；排序后的归并流做相邻去重，
  树页访问顺序、重复候选或临时分片边界都不可能造成重复或漏行。
* 行按 `(source_id, zone_id)` 的 **Unicode 码点序**排列；UTF-8 字节序与
  码点序一致，因此排序键就是 id 的 UTF-8 字节，与进程 locale 无关。
* 数值使用 Python `repr(float64)`：可精确往返的最短表示，不依赖区域设置；
  校验拒绝任何非规范拼写（如 `4` 代替 `4.0`、`inf`、多余空白）。

## 输入校验（只读，绝不修改原目录）

两份输入都是**已提交**的基础目录，构建期以只读 `Catalog` 打开并从其
manifest 推导内容身份（钉住两文件长度与 SHA-256，再做规范哈希）。

* 线目录每个对象必须是 `LineString`/`MultiLineString`，区域目录每个对象
  必须是 `Polygon`/`MultiPolygon`；类型不符、`is_valid` 为假、零长度线
  一律**整体失败**，证据写入 `xt.failed/xtab_rejects.jsonl`（含 role、
  object_id、序号、几何类型、原因码），不跳过记录继续产出。
* 不做投影、吸附、修复或隐式精度网格；坐标完全沿用目录既有的二维平面
  坐标与 Shapely/GEOS 语义。
* GEOS overlay 异常（`GEOSException`）按 `(source_id, zone_id)` 定位失败，
  不吞掉、不继续。

## 候选枚举路线选择（影响正确性的决策）

两条路线都能满足完整语义、缓存预算与确定性输出合同，均已用独立 oracle
验证逐行等价：

| 路线 | 外循环 | 候选来源 | 去重 |
|---|---|---|---|
| **A（已选）线驱动** | 每条线（流式） | 用线 bbox 查**区域目录** R 树 | 配对记录外排 + 相邻去重 |
| B 区域驱动 | 每个区域（流式） | 用区域 bbox 查**线目录** R 树 | 同样的外排 + 相邻去重 |

选择 **A 线驱动**的理由：

1. 每条线的几何在外循环只解码一次，`source_length` 与 bbox 复用；区域
   几何按命中即读即弃。线通常数量更多、单对象更小，外循环持有“一个线
   几何 + 一页区域页”，驻留与线/区域总数无关。
2. 同一 `(线, 区域)` 配对在一次外循环内天然只产生一次，去重主要是合同
   保险；B 路线在区域高度重叠时更容易让同一条线被多个区域重复解码。
3. 两条路线的最坏复杂度相同（全重叠时都退化到 O(L·Z) 次精确求交），
   但 A 的常驻集合更贴合“线多区少”的典型设施/行政边界比例。

被明确否决的伪路线：**“bbox 命中即输出”**（绕过精确几何，违反正长度
语义）与**“限制每条线最多命中数”**（静默截断候选，破坏完整性）。

### 内存/磁盘边界

* 外排序分块 `chunk_bytes ≈ cache_bytes/2`；树页 LRU ≈ `cache_bytes/4`；
  任何时刻驻留 = 一个线几何 + 有限排序块 + 有限页缓存，**全体线、区域、
  候选配对或最终行都不会进入内存**。
* 候选爆发时安全溢写为有序运行文件并多路归并（复用 `spill.py`，
  fan-in 有界）；`--max-spill-bytes` 是显式磁盘预算，超出即在 flush 处
  显式失败（`SpillBudgetExceeded`），**绝不截断候选、绝不发布**。
* 临时文件位于独立 `xt.build-<pid>/tmp`，正常完成、消费者中止与异常
  路径都经 `finally`/故障清理移除；发布只在所有文件写完并 fsync 后，
  以原子写入 manifest 作为提交点。
* `--force` 重建时，既有有效输出先原子挪到备份，任何失败都会恢复它：
  失败构建既不会在最终路径暴露半成品，也不会破坏上一个有效交叉表。

## 实测诊断（`build_report.json` / `xtab diag`）

所有数字均来自真实执行计数：`line_objects`、`zone_objects`、
`bbox_candidates`（树剪枝后的候选数）、`exact_intersections`（精确求交
次数）、`positive_length_pairs`、`zero_length_pairs`、`output_rows`、
`tree_nodes_visited`、`zone_payloads_decoded`、`spill_runs`、
`spill_flush_records`、`spill_bytes_peak`、`rows_tsv_bytes` 及其
SHA-256、`peak_rss_bytes`、`duration_s`。

实测（`xtab diag`，固定种子，运行时生成数据）：

* **稀疏场景**：1,500 线 × 300 区域 = 450,000 个可能配对，仅 220 次
  精确求交（剪枝约 **2000 倍**），输出 189 行；
* **全重叠退化场景**：375 线 × 75 区域，**全部** 28,125 个配对都精确
  求交并全部输出，溢写运行文件归并完成，无任何漏配对——退化被如实报告。

## 校验合同（`xtab validate`）

* 打开即核对 manifest 钉住的 `rows.tsv` 长度；`validate_full` 重算整文件
  SHA-256、逐行解析（头、字段数、规范数值、有限正值、ratio 位一致、
  0 < ratio ≤ 1、intersect ≤ source）、严格码点序与配对唯一性、行数一致；
* 可选地重算两份输入目录的内容身份，身份漂移（输入被重建/篡改）即
  `CorruptionError`；输入路径不可达时显式标注 `unreachable` 而非静默通过。

## 正确性 oracle

`spatial_catalog/brute.py::crosstab_scan` 是独立朴素 O(L·Z) 双重全扫，
**只**调用 Shapely 单件 `intersection().length`，不打开任何目录、不触碰
生产候选枚举/剪枝/归并代码。`tests/test_xtab_differential.py` 对随机小
目录逐行、逐数值差分；规模数据全部运行时生成，不提交大型生成物。


---

# 线设施节点—边拓扑网络（topo）

`window`/`nearest` 回答“哪些 id 命中”，交叉表回答“每段长度归属哪个区域”，
但都无法回答“哪些设施端头相接、网络分成几块、每块包含哪些原始线部件”。
`topo` 把目录里的线设施固化成一份**可直接检查连接关系的节点—边网络**，
而不是又一种查询或报表。
## 连接语义（合同的核心）

* 只有 `LineString`/`MultiLineString` 转成边。`MultiLineString` 按**部件
  序号**拆成多条边，每条边保留来源 `object_id` 与 0 基部件号，因此每个
  部件都可回溯到同一来源对象。
* 一条边只以其**首、尾位置**建立连接端。两个端头在平面二维坐标上
  **数值精确相等**（float64 位相等，`-0.0 == +0.0` 归一）才共用一个节点。
* **没有容差吸附、没有坐标变换、不插入新顶点**：交叉、重叠、相切，或一个
  端头落在另一条线的**内部**，都不产生连接、也不新增顶点。
* **零长度部件**作为自环合法存在：首末端引用同一节点，该节点的端头关联数
  （度数）**增加两次**。
* **重复线仍是两条独立来源边**：它们共用端点节点，但各自是独立的边行。
* 类型不符的记录遵循显式拒收策略：默认整体失败并写证据；加
  `--exclude-non-lines` 时改为排除并在 `selection.jsonl` 与清单计数中留痕。
  被 `--ids-file`/`--id-prefix` 明确排除的记录同样写入运行摘要。
* 错误几何、空部件、无效引用或损坏载荷**绝不静默忽略**；错误信息带
  来源 id 与部件位置（`object_id`、`part`、`position`）。

## 输出目录（版本化、原子提交）

```
topo/
  topo_manifest.json  # 提交点：格式版本、输入目录内容摘要（身份）、
                      #   选择规则、计数、各结果文件长度 + SHA-256（最后原子写入）
  nodes.tsv           # 节点表（两行固定头 + 数据行），按节点键排序
  edges.tsv           # 边表（两行固定头 + 数据行），按 (source_id, part)
                      #   Unicode 码点序排序
  selection.jsonl     # 每个未成为边的被扫描记录及原因
  build_report.json   # 全部为实测计数的构建诊断
```

节点数据行：

```
node_key ⇥ x ⇥ y ⇥ degree ⇥ incident_edges ⇥ component_key ⏎
```

* `node_key`：`N` + 12 位零填充十进制序号，序号按坐标 `(x,y)` 字典序分配；
* `degree`：连到该节点的**边端数**（自环计两次）；
* `incident_edges`：邻边来源引用，形如 `E000000000003@S`（`@S` 首端、
  `@T` 末端），按 `(edge_key, 端)` 严格排序；
* `component_key`：`C` + 该分量中最小节点的序号。

边数据行：

```
edge_key ⇥ source_id ⇥ part_index ⇥ from_node ⇥ to_node ⇥ geometry
         ⇥ planar_length ⇥ component_key ⏎
```

* `edge_key`：`E` + 12 位零填充序号，序号即边在排序表中的位置；
* `from_node`/`to_node`：首尾节点键；自环时二者相同；
* `geometry`：**完整原始坐标序列**的规范 `LINESTRING(...)` 文本（不是仅一个
  引用），可对照源目录逐坐标核验；数值用 Python `repr(float64)`，与
  locale 无关、可精确往返；
* `planar_length`：对原始序列逐段 `hypot` 求和的平面长度（零长度部件为
  `0.0`），校验时按位重算；
* 排序键是 id 的 UTF-8 字节，UTF-8 字节序与 Unicode 码点序一致，因此目录
  物理布局、哈希种子或机器 locale 都不能改变结果；输入不变则多次运行的
  行内容与摘要逐字节稳定。

## 路线选择（满足合同的两种合规路线之一）

| 路线 | 归节点 | 分量合并 |
|---|---|---|
| **甲（已选）端头外排序 + 磁盘并查** | 线部件两端写成记录，外部排序后按坐标归并 | 外部边表 + 驻磁盘的并查状态 |
| 乙 磁盘关系库 | 坐标键与边关系写入关系库，唯一键归节点 | 邻接索引 + 磁盘队列遍历 |

选择**甲**的理由：端头是天然的等长记录，外排序归并能在一趟流式合并里同时
完成“精确相等归节点 + 度数统计”，无需维护任何随节点数增长的驻留映射；
随后的分量闭包交给**驻磁盘并查集**（父/秩数组在定宽文件中，经有界 LRU
页缓存访问），把外排序与并查两套机制都纳入同一个显式内存预算。被否决的伪
方案：全内存 union-find（违反有界 RAM）、按距离容差擅自连边（违反精确相
等）、限制每分量大小（破坏完整连通闭包）。

### 阶段状态与内存/磁盘开销

1. **phase 1 扫描**：流式读目录，一次驻留一个几何；写边记录与端头记录到
   长度前缀原始文件。
2. **phase 2 外排序**：边按 `(source_id, part)`、端头按 `(x,y)` 外排序
   （分块受 `--cache-bytes` 约束，溢出落盘多路归并）；端头合并按位相等
   分组，分配稳定节点序号与度数。
3. **phase 3 连接回填**：节点—端关联重排序后，把节点序号合并回每条边，
   写定宽平面边表。
4. **phase 4 磁盘并查**：`tunion.py` 的父/秩数组驻文件、有界页缓存；
   逐边 `union`，再经若干次定宽记录外排序把根重标为分量最小成员节点。
   每个相同坐标类、每个真实分量都处理完整，不设分量上限。
5. **phase 5 渲染**：按稳定序输出 `edges.tsv`/`nodes.tsv`，交叉核对每个
   度数与邻接数。
6. **phase 6 提交**：全部文件写完并 `fsync`，最后原子写
   `topo_manifest.json` 作为唯一完成标志。

外排序分块 `≈ cache_bytes/2`，并查页缓存 `≈ cache_bytes/8`；任何时刻驻留
为一个几何 + 有限排序块 + 有限并查页，全体节点、边、邻接或分量都不会进入
内存。`--max-spill-bytes` 是显式磁盘预算，超限即在 flush 处显式失败
（`TopologySpillBudgetExceeded`），**绝不截断网络、绝不发布**。

## 失败收敛与原目标保护

* 先在目标旁构造 `topo.build-<pid>/` 暂存结果；只有全部写全并同步后，
  清单才原子可见。异常退出、消费者中断（KeyboardInterrupt）或写满磁盘时，
  暂存目录收敛为 `topo.failed/`（带证据与失败报告），**没有清单即不可被
  消费者误认为已完成**。
* `--force` 重建时，既有有效输出先原子挪到备份；任何失败都原样恢复它，
  失败构建既不会在最终路径暴露半成品，也不会破坏上一个有效网络。

## 校验合同（`topo validate`）

打开即核对清单钉住的各文件长度；`validate_full` 重算全部 SHA-256 并证明：

* 损坏字节、缺失/多余行、重复键、悬空节点/边引用；
* 边端点坐标与其几何首末端**逐坐标一致**，存储长度与重算值**按位相等**；
* 每个节点度数等于边端重算数，邻边引用列表与边表完全一致；
* **分量声明正确**——由一个**独立实现**的朴素内存 BFS（不导入生产端的端头
  归并、磁盘并查或任何遍历代码）重算连通闭包逐节点对照；
* 可选重算源目录内容身份并把每条边对照源载荷（深几何一致性）；身份漂移即
  `CorruptionError`，路径不可达时显式标注 `unreachable` 而非静默通过；
* `selection.jsonl` 的排除/拒收计数与 `build_report.json`、清单计数一致。

## 实测诊断（`build_report.json` / `topo diag`）

所有数字来自真实执行：`records_scanned`、`records_excluded_filter`、
`records_type_rejected`、`parts_parsed`、`zero_length_parts`、
`endpoint_records`、`nodes`、`edges`、`components`、`spill_runs`、
`spill_bytes_peak`、`tmp_bytes_written`、两 TSV 字节数与 SHA-256、
`union_find_finds/unions/evictions`、`peak_rss_bytes`（进程峰值 RSS）。

规模场景含**大量彼此分离的小组件**和**一个超大分量**（远超并查页缓存），
用以检查有界资源处理与分量完整性；`topo diag` 还对每个节点、每条边运行
独立朴素 oracle（`topo_oracle.py`，朴素坐标映射 + 显式栈图遍历，与生产代码
零共享）差分。所有规模数据运行时生成，不提交大体量 fixture。


---

# 巡检覆盖对账（inspcov）

巡检班组按路段桩号登记结果，方向可能与底图相反；月报发布后又常收到补录、
更正和撤回。`topo` 的边表回答了“网络里有哪些路段”，却无法回答“上次月报
关账时我们知道什么、这次重算后哪些区段补齐了/重新缺检了/只是换了证据”。
`inspcov` 把同一报告期、两个收件截止号上的巡检记录固化成**两份覆盖台账和
一份逐区段差异**，供维护员据此安排复查。拓扑只充当路段名册：不重算节点、
连通分量或几何相交，也不把一条路段的巡检传到相邻路段。

## 输入契约

* **拓扑产物**（`topo build` 的目录）：只读打开，用其 `edges.tsv` 验证登记表
  引用的边确实存在、且不是零长度几何边，并钉住其内容身份（见下）。
* **桩号登记表**（JSONL）：每行
  `{"source_id": ..., "part_index": ..., "chainage_end": L}`。`L` 为正整数
  桩号终点（≤ 有符号 64 位上限）。纳管范围就是登记表列出的边，不要求覆盖
  整个网络。**重复登记、引用不存在的边、引用零长度几何边一律整体失败**。
  桩号是维护台账的整数计量单位，**不从浮点几何长度换算**。
* **巡检文件**（一个或多个无序 UTF-8 JSONL）：每行一个修订，以
  `inspection_id` 标识一条观察，带正整数 `batch`，`action` 为 `upsert`（整条
  观察）或 `retract`（撤回，不带观察内容；撤回一个从未见过的编号是合法的
  无效化声明）。`upsert` 字段：`source_id`、`part_index`、`direction`
  （`forward`/`reverse`）、整数区间 `[lo, hi)`（`0 <= lo < hi <= L`）、覆盖
  日期区间 `[start, end)`、巡检日期 `checked_on`、非负整数 `priority`、结果
  `pass`/`defect`。日期为严格公历 `YYYY-MM-DD`，区间不得为空；所有输入整数
  不得超过有符号 64 位上限。
  * 同一 `(inspection_id, batch)` 字段值完全相同是**重复送达**，只算一次；
    字段值不同是**冲突**，构建失败并在证据中给出原始文件与行号。
  * 方向沿拓扑边坐标序列时直接使用桩号；逆向时把区间映射为
    `[L-hi, L-lo)`。
  * 覆盖日期与收件批次是两根不同的时间轴：**不用日期推断收件先后**。

## 两次关账的重现语义

给定非空报告窗口 `[from, to)` 与两个 `0 <= before <= after` 的收件截止号。
在每个截止号：

1. 在全部收件中为每个编号选出**不晚于截止号的最大批次**修订；
2. 去掉撤回（撤回即无效化该编号在该截止号的观察）；
3. 裁剪到报告期（与 `[from, to)` 求交，空则该观察不贡献）。

更正可搬到别的边、日期或桩号：**不能只用更正覆盖原区间的重叠部分**（旧版本
整体被取代），也**不能因为最新版本落在报告期外就把旧版本捡回来**。

对纳管边的每个日期与桩号位置，重叠观察只取一条：**优先级高者先，其次巡检
日期较晚者先，仍相同则取 `inspection_id` Unicode 码点序较小者**。没有胜出
记录就是 `gap`（包括一条观察都没有的纳管边）。输出附胜出的
`(inspection_id, batch)`，因此可回查是哪次更正或补录提供了结果——**收件较新
不意味着优先级更高**。

## 输出目录（版本化、原子提交）

```
inspcov/
  inspcov_manifest.json  # 提交点：格式版本、拓扑身份、登记表与各巡检
                         #   文件摘要、两个截止号、报告窗口、各输出文件
                         #   长度 + SHA-256（最后原子写入）
  managed.tsv            # 纳管边名册（source_id, part_index, chainage_end）
  coverage_before.tsv    # before 截止号覆盖台账（矩形行）
  coverage_after.tsv     # after 截止号覆盖台账
  diff.tsv               # 逐区段差异（前后两侧状态与证据）
  summary.json           # 机器可读小结（状态对 + 仅证据替换，桩号单位×天数）
  build_report.json      # 全部为实测计数的构建诊断
```

覆盖台账**完整铺满**各纳管边 `[from, to) × [0, L)`，每行是一个带日期起止与
桩号起止的矩形，携带状态与证据键（`gap` 无证据）。归并规则：

* 同一天相邻、状态和证据完全相同的桩号段合为一段；
* 相邻日期的**整条边剖面**相同时合并为一个日期段。

差异表按同样的归并方式输出两个截止号**状态或证据不同**的位置，每行携带前后
两侧。差异的日期剖面是该日差异段的完整有序集合；**空剖面不输出，且不允许
跨过空剖面合并**。不直接比较两份表的行键推断变化，也不把只换证据的区段报成
状态变化。小结按状态对（如 `defect>pass`）与仅证据替换（如 `pass>pass`）
分别累计变化的“桩号单位 × 天数”，**一处变化只计一次**。

排序：同一边按日期起点、再按正向桩号起点；边按 `(source_id, part_index)`
排序，字符串用 Unicode 码点序（UTF-8 字节序与之一致，与 locale 无关）。

## 内存/磁盘边界

这是可能超过内存的全网数据处理：一条干线自身的修订、重叠观察和最终剖面也
可能很大。构建**不把**完整修订历史、单边全部活动记录、单个巨大剖面或结果表
常驻内存，也**不逐天逐桩号展开**、不物化所有日期端点与所有桩号端点的笛卡尔
格。覆盖计算、跨截止号差异对齐和输出归并都在显式预算下工作，否则明确失败：

| 预算 | 作用域 |
|---|---|
| `--cache-bytes` | 外排序分块（≈一半/块）、扫描堆与剖面存储的内存上限 |
| `--max-spill-bytes` | 显式磁盘暂存上限，超限即在 flush 处显式失败，绝不发布部分月报 |

* 修订、可见观察、日期端点、登记表都走**外排序**（分块排序 + 多路归并）；
* 每条边的日期×桩号扫描用**可溢写堆**（`SpillableHeap`）保存活动观察与胜者
  候选，超出内存上限即落盘为有序运行文件并归并；
* 剖面归并用**可溢写剖面存储**，逐区间与上一区间比对，相同则并入日期段；
* 结果行流式写出，任何时刻驻留 = 有限排序块 + 有限堆 + 有限剖面。

## 实测统计（`build_report.json` / `inspcov diag`）

全部为真实执行计数：`topology_edges_scanned`、`registry_lines`、
`managed_edges`、`revision_files`、`revision_lines`、`revisions_upsert`、
`revisions_retract`、`duplicate_deliveries`、`visible_observations`、
`observations_clipped_empty`、`elementary_date_intervals`、
`observation_activations`（扫描处理的观察边界实例）、`cov_before_segments`/
`cov_after_segments`/`diff_segments`（归并前最细段）、`date_runs_*`、
`coverage_rows_*`、`diff_rows`（实际交付行）、`spill_runs`、`heap_spills`、
`heap_high_water`、`spill_bytes_peak`、`tmp_bytes_written`、`peak_rss_bytes`。
**隐藏在高优先级观察下的边界**（被遮蔽、未成为交付行的边界）由
`observation_activations`、最细段数与交付行数之差体现，与实际交付行区分。

## 失败收敛与原目标保护

* 先在目标旁构造 `inspcov.build-<pid>/`；只有全部写全并 fsync 后，清单才原子
  可见。坏行、未知引用、损坏的被消费拓扑边表、修订冲突、暂存写失败都会使
  构建失败并收敛为 `inspcov.failed/`（带证据与失败报告），**绝不发布看似成功
  的部分月报**。
* `--force` 重建时，既有有效输出先原子挪到备份；任何失败都原样恢复它——
  失败既不会暴露半成品，也不会破坏上一份有效对账。
* 临时数据全部在 `*.build-<pid>/tmp` 内，正常完成与失败路径都会清理，不入 Git。

## 校验合同（`inspcov validate`）

打开即核对清单钉住的各文件长度；`validate_full` 重算全部 SHA-256 并证明
（**不要求重跑拓扑构建**）：

* 每行头部、字段数、规范编码、日期/区间/状态词表合法；
* 行严格按 `(source_id, part_index)` 码点序、再按日期起点、桩号起点排列；
* 每份覆盖台账对每个纳管边**无缺无重**铺满 `[from, to) × [0, L)`（窗口取自
  清单，`L` 取自 `managed.tsv`）；
* 差异与两份台账一致：用一个**独立实现**的对齐器从 `coverage_before` 与
  `coverage_after` 重新逐格推出差异矩形，要求与 `diff.tsv` 逐行相等；
* `summary.json` 的状态对与仅证据替换合计等于从 `diff.tsv` 重算的合计；
* 清单中的批次参数（两个截止号、报告窗口）与交付内容一致；
* 可选地把 `managed.tsv` 对照登记表、把拓扑内容身份对照清单钉住值；路径不可
  达时显式标注 `unreachable` 而非静默通过。

## 为什么覆盖与差异的表示不受修订或遍历次序影响

* **修订选择是集合函数**：每个编号在两个截止号上的胜出修订只取决于
  `(inspection_id, batch)` 集合（不晚于截止号的最大批次），与这些修订写在
  哪些文件、以什么顺序读到无关。重复送达按内容去重，冲突按内容报错，因此
  同一批次的记录如何分放、如何到达都收敛到同一个“可见修订集”。
* **覆盖是逐格全序**：每个 `(日期, 桩号)` 格的胜者由全序键
  `(优先级, 巡检日期, inspection_id)` 唯一决定，与计算顺序无关；`gap` 是
  “没有胜者”的确定结果。
* **归并是规范形式**：桩号方向取“相邻等值即并”的极大段，日期方向取“整边
  剖面相同即并”的极大段，都是格值函数的唯一规范分解，与扫描到边界的先后
  无关。被高优先级观察遮蔽的边界本就不改变格值，自然被归并吸收。
* **差异是格值差**：差异先逐格比较两份覆盖，再做同样的规范归并；因此差异
  与小结是两个截止号各自“可见修订集”的纯函数。
* **预算只改变数据落点**：外排序分块大小、堆与剖面是否溢写，只影响数据在
  内存还是磁盘，不改变排序后的记录流、扫描结果或归并结果——不同
  `--cache-bytes` 下业务输出逐字节相同（测试与诊断都验证了这一点）。

## 正确性 oracle

`spatial_catalog/ioracle.py` 是独立的逐格参考实现：把小整数桩号、短日期窗口
的每个 `(日期, 桩号)` 格展开，用最朴素的全扫描选胜者，再独立归并出台账、
差异与小结，与生产代码**零共享**。`tests/test_insp_differential.py` 对随机小
场景逐点比较前后状态、证据与变化合计，并单独核对矩形归并的规范性；
`tests/test_insp_scale.py` 在含一条长尾干线、大批被遮蔽交错观察与大量分散
路段的运行时生成场景上，比较不同预算下的业务输出并验证被迫落盘与资源用尽
行为。所有规模数据运行时生成，不提交大体量 fixture。
