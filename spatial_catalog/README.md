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

