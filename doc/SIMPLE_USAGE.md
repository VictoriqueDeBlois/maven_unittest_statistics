# 简单使用说明

当前推荐只使用一个入口：

```bash
uv run python run_balanced_benchmark_pipeline.py
```

脚本会自动检查已有产物，已存在且非空的步骤会跳过。

## 查看执行计划

```bash
uv run python run_balanced_benchmark_pipeline.py --dry-run
```

## 常用重跑方式

```bash
# 强制全量重跑
uv run python run_balanced_benchmark_pipeline.py --force

# 只重跑 benchmark 筛选
uv run python run_balanced_benchmark_pipeline.py --force-step benchmark

# 只生成 CSV/统计，不抽取测试代码
uv run python run_balanced_benchmark_pipeline.py --skip-extract
```

## 当前目录约定

```text
data/input/          输入列表和人工黑名单
data/intermediate/   中间 CSV 和 workdir
logs/                运行日志
outputs/             最终 benchmark
archive/             旧代码、旧产物、旧日志归档
```

最终结果位于：

```text
outputs/balanced_benchmark_representative/
```
