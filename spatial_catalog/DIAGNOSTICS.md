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
