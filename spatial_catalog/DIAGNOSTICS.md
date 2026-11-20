# 诊断输出（可复现）

复现命令（仓库根目录，Python 3.11 + Shapely 1.8.5）：

```bash
python3 -m spatial_catalog diag --out /tmp/diag-full --n 100000 --cache-bytes 8388608
python3 -m pytest tests/test_scale.py -s
```

## `diag`（n=100000，cache 8 MiB，种子 7；数据运行时生成）

| 场景 | 指标 | 结果 |
|---|---|---|
| 稀疏构建 | 对象 / 深度 / 页数（叶） | 100000 / 3 / 1099 (1087) |
| 稀疏构建 | nodes.bin / payloads.bin | 4.3 MiB / 6.8 MiB |
| 稀疏构建 | 耗时 | 10.9 s |
| 稀疏 window ×30 | 平均访问节点 | **4.2 / 1099 页（0.4%）** |
| 稀疏 window ×30 | 平均解码载荷 | **12.7 / 100000（0.013%）** |
| 稀疏 nearest ×30 | 平均访问节点 | **4.3 / 1099 页** |
| 稀疏 nearest ×30 | 平均解码载荷 | **8.1 / 100000（0.008%）** |
| 全重叠构建 | 对象 / 页数 | 20000 / 222 |
| 全重叠 window ×10 | 平均访问节点 / 载荷 | 136.1 页 / 3756.6（退化，如实报告） |
| 全重叠 nearest ×10 | 平均访问节点 / 载荷 | 134.7 页 / 3835.1（退化，如实报告） |
| 全程 | 峰值 RSS | 120.3 MiB |

`diag` 内每个查询都与独立顺序全扫（`brute.py`）逐条断言一致；
统计字段来自真实遍历计数。

## `test_scale.py` 实测（pytest -s 输出）

```
[sparse build] n=60000 cache=4MiB rss_delta=10.3MiB pages=661 depth=3 duration=6.8s
[sparse window] avg nodes=4.5/661 pages, avg payloads=1.8/60000
[sparse nearest] avg nodes=4.5, avg payloads=10.1/60000
[overlap nearest] avg nodes=41.6/45 avg payloads=1676.2/4000 (degenerate as expected)
[overlap] peak rss so far: 112.4 MiB
```

- 稀疏：window/nearest 的节点与载荷访问量约为全扫的 **万分之几**，
  证明确实走索引剪枝（断言 `avg < n/100`）。
- 构建有界内存：60k 几何、4 MiB 预算，构建期 RSS 增量 **10.3 MiB**
  （外排/溢写生效；断言 < 1 GiB）。
- 全重叠最坏情况：剪枝失效，中心 window 命中全部 4000 件、nearest
  平均解码 1676/4000 载荷——测试断言正确性并**如实打印退化**，
  不做性能粉饰。
- 另有 20 万件构建冒烟测试（`test_build_one_million_scale_smoke`）
  验证溢写管线在更大输入下保持正确。

注：`diag` 中的 `sparse_build_rss_delta_bytes` 可能读作 0——
`ru_maxrss` 是单调峰值，数据生成阶段已抬高；构建期真实增量以
`test_scale.py` 的测量为准。

---

# 交叉表诊断（线—区域长度归属）

复现命令（数据全部运行时生成，固定种子，不提交大型生成物）：

```bash
python3 -m spatial_catalog xtab diag --out /tmp/xtab-diag \
    --n-lines 2000 --n-zones 500 --cache-bytes 4194304 --seed 21
python3 -m pytest tests/test_xtab_scale.py -s
```

## `xtab diag` 实测（2000 线 × 500 区域；cache 4 MiB；种子 21）

| 场景 | 指标 | 结果 |
|---|---|---|
| 稀疏 | 可能配对 O(L·Z) | 1,000,000 |
| 稀疏 | bbox 候选 / 精确求交 | 380 / 380（**剪枝约 2632×**） |
| 稀疏 | 正长度配对（输出行） | 337（另有 43 个零长度接触，不出行） |
| 稀疏 | 树节点访问 / 区域载荷解码 | 2793 / 380 |
| 稀疏 | 溢写运行文件 / 峰值溢写 | 1 / 17,524 B |
| 全重叠 | 线 × 区域 | 500 × 125 = 62,500 |
| 全重叠 | bbox 候选 / 精确求交 / 输出行 | **62,500 / 62,500 / 62,500（无剪枝，全配对）** |
| 全重叠 | 溢写运行文件 / 峰值溢写 | 2 / 3,375,000 B（外排归并完成，无截断） |
| 全程 | 峰值 RSS | ~69 MiB |

两个场景的输出都在 `xdiag.py` 内与独立朴素 O(L·Z) 双重全扫 oracle
（`brute.crosstab_scan`，只用 Shapely 单件 `intersection().length`）
逐行、逐数值断言一致。

## `test_xtab_scale.py` 实测（pytest -s 输出）

```
[sparse xtab] lines=12000 zones=2000 total_pairs=24000000 candidates=1483
              exact=1483 rows=1282 rss_delta=1.3MiB spill_runs=1 spill_peak=66664
[overlap xtab] pairs=32000 spill_runs=25 spill_peak=1728000 rows=32000
```

- 稀疏：2400 万可能配对仅 1483 次精确求交（断言 `< total/50`），
  2 MiB 预算下构建期 RSS 增量约 1.3 MiB，证明有界内存 + 真实剪枝。
- 全重叠：800 × 40 = 32,000 配对全部精确求交并全部输出，64 KiB 级
  排序块强制 25 个溢写运行文件后多路归并——退化被如实报告且零漏配对。
- 计数器不变量由构建器自检：`positive + zero == exact`、
  `rows + dedup_dropped == positive`，违反即失败不发布。


---

# 巡检覆盖对账诊断（inspcov）

复现命令（数据全部运行时生成，固定种子，不提交大型生成物）：

```bash
python3 -m spatial_catalog inspcov diag --out /tmp/insp-diag \
    --n-edges 400 --trunk-obs 4000 --cache-bytes 1048576 --seed 47
```

## `inspcov diag` 实测（400 分散路段 + 1 条长尾干线（4000 条交错观察）；cache 1 MiB；种子 47）

场景：401 条纳管边、6461 行修订（批次 1..6，含更正与撤回），报告窗口
14 天，截止号 3 与 6。

| 指标 | 结果 |
|---|---|
| 输入修订行 | 6,461 |
| 可见观察（before/after 去重后记录） | 5,689（before 3,878 / after 4,957） |
| 基本日期区间（处理的日期边界） | 2,270 |
| 观察激活（扫描处理的观察边界实例） | 21,653 |
| 归并前最细段（before/after/diff） | 5,549 / 6,970 / 1,466 |
| 交付行（before/after/diff） | 4,330 / 6,569 / 1,278 |
| 日期段（before/after/diff） | 1,645 / 2,122 / 793 |
| 外排溢写运行文件 / 扫描堆溢写 | 6 / 26 |
| 扫描堆峰值驻留 | 1,903 条 |
| 暂存峰值 / 临时写出 | 2.66 MiB / 2.34 MiB |
| 峰值 RSS | 43.8 MiB |
| 构建耗时 | ~1.0 s |

* **隐藏边界 vs 交付行**：21,653 次观察激活、5,549/6,970 个最细段，最终只
  交付 4,330/6,569 行——被高优先级观察遮蔽的边界（未成为交付行）由
  激活数、最细段数与交付行数之差如实体现。
* **预算无关性**：同一场景在 1 MiB 与 8 MiB 预算下各构建一次，五份业务
  输出（managed、coverage_before、coverage_after、diff、summary）的
  SHA-256 逐字节一致（`budget_independence.identical = true`）。
* **独立校验**：`inspcov validate` 的独立对齐器从两份台账重推差异并与
  `diff.tsv` 逐行相等；`ioracle.py` 逐格 oracle 对一个小型子场景逐点
  断言一致（`oracle_diff.ok = true`）。
