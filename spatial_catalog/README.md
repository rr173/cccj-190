# spatial-catalog — 离线区域边界/线设施磁盘空间目录

为离线采集的区域边界与线设施提供**静态构建、只读查询**的磁盘空间索引。
输入是无序、可大于内存的 JSONL（每行一个 `object_id` + GeoJSON 几何），
构建出自研的持久化 Hilbert 装箱 R 树；查询支持闭矩形 window 与精确 kNN
nearest。包围盒只用于剪枝，**业务答案永远由几何真值决定**。

## 安装与运行

依赖：Python ≥ 3.9 + Shapely（1.8 或 2.x，仅用于单件几何的合法性判断、
`intersects`、点到几何精确距离；**不**使用 STRtree、空间数据库或任何
现成索引建树）。

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
