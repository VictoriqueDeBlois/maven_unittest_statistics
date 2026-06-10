# Maven Unit Test Statistics

这个仓库当前的主入口是 `run_balanced_benchmark_pipeline.py`，用于端到端生成 `balanced_benchmark_representative`。

## 当前主流程

```bash
uv run python run_balanced_benchmark_pipeline.py
```

脚本会按顺序执行，并在产物已存在且非空时自动跳过：

1. 扫描 Maven 测试，生成测试方法级指标。
2. 分析本地 Maven 项目，生成项目级元数据。
3. 记录每个项目当前 Git commit。
4. 筛选 representative balanced benchmark。
5. 抽取 selected tests 的 annotated/raw Java 代码。

常用命令：

```bash
# 只查看会执行哪些步骤
uv run python run_balanced_benchmark_pipeline.py --dry-run

# 强制全量重跑
uv run python run_balanced_benchmark_pipeline.py --force

# 只强制重跑某一步
uv run python run_balanced_benchmark_pipeline.py --force-step benchmark

# 只生成 CSV/统计，不抽取测试代码
uv run python run_balanced_benchmark_pipeline.py --skip-extract
```

## 目录布局

```text
data/
  input/
    all_repos.txt
    bad_projects.txt
  intermediate/
    all_tests_jar.csv
    all_tests_jar_workdir/
    projects_stats_refined.csv
    repo_commit_times.csv

logs/
  maven_test_metrics.log
  maven_project_run_refined.log
  extract_test_snippets.log

outputs/
  balanced_benchmark_representative/
    balanced_tests.csv
    selected_projects.csv
    summary.txt
    charts/
    testcases/
      annotated/
      raw_java/

archive/
  legacy_code_20260610/
  legacy_outputs_20260610/
  legacy_logs_20260610/
```

## 主流程代码

当前主流程依赖这些代码文件：

```text
run_balanced_benchmark_pipeline.py
maven_test_metrics_jar.py
analyze_maven_projects.py
get_repo_commit_times.py
build_balanced_benchmark.py
extract_test_snippets.py
select_complex_integration_tests.py
utils/logger_manager.py
```

旧流程脚本已经归档到 `archive/legacy_code_20260610/`。

## 关键产物

最终 benchmark 位于：

```text
outputs/balanced_benchmark_representative/
```

最重要的文件：

```text
outputs/balanced_benchmark_representative/balanced_tests.csv
outputs/balanced_benchmark_representative/selected_projects.csv
outputs/balanced_benchmark_representative/summary.txt
```

更详细的筛选规则和运行机制见：

```text
doc/BALANCED_BENCHMARK_REPRESENTATIVE.md
```
