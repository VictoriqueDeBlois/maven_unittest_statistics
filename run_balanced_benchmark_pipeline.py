#!/usr/bin/env python3
"""运行 representative benchmark 的完整可恢复流水线。

总体流程：
  1. all_repos.txt + 本地仓库 -> all_tests_jar.csv
  2. 本地仓库 -> projects_stats_refined.csv
  3. all_tests_jar.csv + 本地仓库 -> repo_commit_times.csv
  4. 三个 CSV + bad_projects.txt -> balanced_benchmark_representative/*.csv
  5. balanced_tests.csv + 本地仓库 -> annotated/raw Java 测试代码

每一步都会先检查自己的目标产物。目标产物已经存在且非空时，会自动跳过；
除非传入 --force 或 --force-step。
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import analyze_maven_projects
import build_balanced_benchmark
import extract_test_snippets
import get_repo_commit_times
import maven_test_metrics_jar


DEFAULT_JAR = "/data/xuhaoran/idea/maven-test-metrics-java/target/maven-test-metrics-1.0-SNAPSHOT.jar"
DEFAULT_PROJECTS_ROOT = "/data/xuhaoran/github"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "端到端生成 balanced_benchmark_representative。"
            "默认跳过已经存在的中间产物。"
        )
    )
    parser.add_argument("--projects-list", default="all_repos.txt")
    parser.add_argument("--projects-root", default=DEFAULT_PROJECTS_ROOT)
    parser.add_argument("--all-tests-csv", default="all_tests_jar.csv")
    parser.add_argument("--projects-csv", default="projects_stats_refined.csv")
    parser.add_argument("--projects-log", default="maven_project_run_refined.log")
    parser.add_argument("--reuse-llm-csv", default=None)
    parser.add_argument("--reuse-stats-csv", default=None)
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument("--commits-csv", default="repo_commit_times.csv")
    parser.add_argument("--exclude-projects", default="bad_projects.txt")
    parser.add_argument("--output-dir", default="balanced_benchmark_representative")
    parser.add_argument("--jar", default=DEFAULT_JAR)
    parser.add_argument("--metrics-workers", type=int, default=20)
    parser.add_argument("--extract-workers", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--target-projects", type=int, default=50)
    parser.add_argument("--tests-per-project", type=int, default=5)
    parser.add_argument("--min-count", type=int, default=200)
    parser.add_argument("--max-count", type=int, default=400)
    parser.add_argument("--min-packages", type=int, default=5)
    parser.add_argument("--min-llm-confidence", type=float, default=0.70)
    parser.add_argument("--max-compile-time-seconds", type=int, default=1800)
    parser.add_argument("--max-huge-projects", type=int, default=6)
    parser.add_argument(
        "--exclude-size-level",
        action="append",
        default=[],
        help="透传给 build_balanced_benchmark.py；可以重复传入。",
    )
    parser.add_argument(
        "--skip-extract",
        action="store_true",
        help="只生成 CSV/统计产物，不抽取 annotated/raw Java 测试代码。",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使产物已存在，也强制重跑所有步骤。",
    )
    parser.add_argument(
        "--force-step",
        action="append",
        choices=["metrics", "projects", "commits", "benchmark", "annotated", "raw-java"],
        default=[],
        help="只强制重跑指定步骤，其它步骤仍按产物是否存在决定是否跳过。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将要执行的命令，不实际运行。",
    )
    return parser.parse_args()


def file_ready(path: Path) -> bool:
    """CSV 一类的中间文件：存在且非空，就认为已经生成完毕。"""
    return path.is_file() and path.stat().st_size > 0


def dir_ready(path: Path) -> bool:
    """代码抽取目录：目录存在且里面有内容，就认为已经生成完毕。"""
    return path.is_dir() and any(path.iterdir())


def should_run(step: str, output: Path, args: argparse.Namespace, *, is_dir: bool = False) -> bool:
    """根据 force 参数和目标产物状态，判断某一步是否需要运行。"""
    if args.force or step in set(args.force_step):
        return True
    return not (dir_ready(output) if is_dir else file_ready(output))


def run_module_main(module_main, argv: list[str], args: argparse.Namespace) -> None:
    """像 main.py 一样 patch sys.argv，然后在当前进程里调用其它脚本的 main()。"""
    print("\n$ " + " ".join(argv), flush=True)
    if args.dry_run:
        return
    with patch("sys.argv", argv):
        result = module_main()
    if isinstance(result, int) and result != 0:
        raise RuntimeError(f"Step failed with exit code {result}: {' '.join(argv)}")


def run_metrics(args: argparse.Namespace) -> None:
    """第 1 步：扫描 Maven 测试，生成测试方法级指标表。

    输入：
      - args.projects_list：项目列表文件，每行一个 owner/repo。
      - args.projects_root：本地仓库根目录，下面是 owner/repo 目录。
      - args.jar：Java 侧测试指标分析 jar。

    输出：
      - args.all_tests_csv，默认 all_tests_jar.csv。
      - maven_test_metrics_jar 还会在 CSV 旁边维护 workdir，用于记录
        每个项目的进度和断点续跑状态。
    """
    output = Path(args.all_tests_csv)
    if not should_run("metrics", output, args):
        print(f"[skip] metrics already exists: {output}")
        return

    run_module_main(
        maven_test_metrics_jar.main,
        [
            "maven_test_metrics_jar.py",
            "--projects",
            args.projects_list,
            "--root",
            args.projects_root,
            "--output",
            args.all_tests_csv,
            "--workers",
            str(args.metrics_workers),
            "--jar",
            args.jar,
            "--timeout",
            str(args.timeout),
        ],
        args,
    )


def run_project_analysis(args: argparse.Namespace) -> None:
    """第 2 步：分析每个项目，生成项目级元数据。

    输入：
      - args.projects_root：本地仓库根目录。
      - 可选 --reuse-llm-csv/--reuse-stats-csv，用于避免重复做 LLM 标注。

    输出：
      - args.projects_csv，默认 projects_stats_refined.csv。
      - args.projects_log，默认 maven_project_run_refined.log。

    后续 balanced selector 会使用 Maven 有效性、项目类型标签、编译规模、
    预计编译时间、SLOC、LLM 置信度等字段。
    """
    output = Path(args.projects_csv)
    if not should_run("projects", output, args):
        print(f"[skip] project stats already exists: {output}")
        return

    cmd = [
        "analyze_maven_projects.py",
        "--projects-root",
        args.projects_root,
        "--output-csv",
        args.projects_csv,
        "--log-file",
        args.projects_log,
    ]
    if args.skip_llm:
        cmd.append("--skip-llm")
    if args.reuse_llm_csv:
        cmd.extend(["--reuse-llm-csv", args.reuse_llm_csv])
    if args.reuse_stats_csv:
        cmd.extend(["--reuse-stats-csv", args.reuse_stats_csv])
    run_module_main(analyze_maven_projects.main, cmd, args)


def run_commit_times(args: argparse.Namespace) -> None:
    """第 3 步：记录测试表中每个项目当前对应的 Git commit。

    输入：
      - args.all_tests_csv：用于提取唯一 project_name。
      - args.projects_root：每个项目 .git 目录所在的本地仓库根目录。

    输出：
      - args.commits_csv，默认 repo_commit_times.csv。

    这些 commit 字段会合并进最终 benchmark CSV，保证每条选中测试都能追溯到
    当时分析的具体仓库版本。
    """
    output = Path(args.commits_csv)
    if not should_run("commits", output, args):
        print(f"[skip] commit times already exists: {output}")
        return

    print(
        "\n$ "
        + (
            "get_repo_commit_times.main("
            f"csv_path='{args.all_tests_csv}', "
            f"repos_root='{args.projects_root}', output_csv='{args.commits_csv}')"
        ),
        flush=True,
    )
    if args.dry_run:
        return
    get_repo_commit_times.main(
        csv_path=args.all_tests_csv,
        repos_root=args.projects_root,
        output_csv=args.commits_csv,
    )


def run_benchmark(args: argparse.Namespace) -> None:
    """第 4 步：筛选 representative balanced benchmark。

    输入：
      - args.all_tests_csv：测试方法级指标。
      - args.projects_csv：项目级元数据和项目类型标签。
      - args.commits_csv：commit hash/time 元数据。
      - args.exclude_projects：可选的 owner/repo 黑名单。

    输出到 args.output_dir 下：
      - balanced_tests.csv
      - balanced_tests_easy.csv / medium / hard / expert
      - selected_projects.csv
      - summary.txt
      - excluded_projects.log
      - charts/
    """
    output = Path(args.output_dir) / "balanced_tests.csv"
    if not should_run("benchmark", output, args):
        print(f"[skip] balanced benchmark already exists: {output}")
        return

    cmd = [
        "build_balanced_benchmark.py",
        "--tests",
        args.all_tests_csv,
        "--projects",
        args.projects_csv,
        "--commits",
        args.commits_csv,
        "--output-dir",
        args.output_dir,
        "--target-projects",
        str(args.target_projects),
        "--tests-per-project",
        str(args.tests_per_project),
        "--min-count",
        str(args.min_count),
        "--max-count",
        str(args.max_count),
        "--min-packages",
        str(args.min_packages),
        "--min-llm-confidence",
        str(args.min_llm_confidence),
        "--max-compile-time-seconds",
        str(args.max_compile_time_seconds),
        "--max-huge-projects",
        str(args.max_huge_projects),
    ]
    if args.exclude_projects and Path(args.exclude_projects).exists():
        cmd.extend(["--exclude-projects", args.exclude_projects])
    for size_level in args.exclude_size_level:
        cmd.extend(["--exclude-size-level", size_level])
    run_module_main(build_balanced_benchmark.main, cmd, args)


def run_extract(args: argparse.Namespace, fmt: str, output_name: str, step: str) -> None:
    """第 5 步：从源码仓库中抽取已选测试代码。

    输入：
      - args.output_dir/balanced_tests.csv。
      - args.projects_root：本地仓库根目录。
      - args.jar：extract_test_snippets 的 jar 模式会使用它。

    输出：
      - fmt 为 annotated 时，输出到 args.output_dir/testcases/annotated。
      - fmt 为 raw-java 时，输出到 args.output_dir/testcases/raw_java。
    """
    output = Path(args.output_dir) / "testcases" / output_name
    if not should_run(step, output, args, is_dir=True):
        print(f"[skip] extracted {fmt} testcases already exist: {output}")
        return

    run_module_main(
        extract_test_snippets.main,
        [
            "extract_test_snippets.py",
            "--csv",
            str(Path(args.output_dir) / "balanced_tests.csv"),
            "--root",
            args.projects_root,
            "--output",
            str(output),
            "--mode",
            "all",
            "--workers",
            str(args.extract_workers),
            "--jar",
            args.jar,
            "--format",
            fmt,
        ],
        args,
    )


def validate_required_inputs(args: argparse.Namespace) -> None:
    """提前检查固定外部输入；这些输入无法由本流水线自动创建。"""
    required = [
        ("projects list", Path(args.projects_list)),
        ("projects root", Path(args.projects_root)),
        ("metrics jar", Path(args.jar)),
    ]
    missing = [f"{label}: {path}" for label, path in required if not path.exists()]
    if missing:
        raise FileNotFoundError("Missing required input(s):\n" + "\n".join(missing))


def main() -> int:
    args = parse_args()
    validate_required_inputs(args)

    # 顺序不能换：后面的步骤会读取前面步骤生成的文件。
    run_metrics(args)
    run_project_analysis(args)
    run_commit_times(args)
    run_benchmark(args)

    if not args.skip_extract:
        run_extract(args, "annotated", "annotated", "annotated")
        run_extract(args, "raw-java", "raw_java", "raw-java")

    print("\nPipeline complete.")
    print(f"Output directory: {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
