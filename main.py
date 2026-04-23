import maven_test_metrics
import extract_test_snippets
import run_maven_tests
import filter_integration_tests
import maven_test_metrics_jar
import select_complex_integration_tests
import sample_by_project

from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv


def collect_all_tests():
    load_dotenv()
    args = [
        '--projects', 'all_repos.txt',
        '--root', '/data/xuhaoran/github',
        '--output', 'all_tests.csv'
    ]
    with patch('sys.argv', ['main.py'] + args):
        maven_test_metrics.main()

    args = [
        '--input', 'all_tests.csv',
        '--output', 'all_integration_code.csv',
    ]
    with patch('sys.argv', ['main.py'] + args):
        filter_integration_tests.main()

    args = [
        '--csv', 'all_integration_code.csv',
        '--root', '/data/xuhaoran/github',
        '--output', 'all_integration_tests',
        '--mode', 'all'
    ]
    with patch('sys.argv', ['main.py'] + args):
        extract_test_snippets.main()


def build_integration_benchmark(
    all_tests_csv: str = 'all_tests_jar.csv',
    selected_csv: str = 'selected_integration_tests_v7.csv',
    benchmark_csv: str = 'integration_benchmark_v7_n10.csv',
    min_packages: int = 7,
    max_per_project: int = 10,
    exclude_mock: bool = True,
):
    """
    构建复杂集成测试 benchmark 的完整调用链。

    步骤：
      1. 运行 maven_test_metrics_jar 分析所有测试（如 all_tests_jar.csv 已存在可跳过）
      2. 用激进方案筛选（called_packages_count >= N, no mock）
      3. 项目级采样（每项目取 Top-K）
    """
    load_dotenv()

    import os
    print(f"JAVA_HOME = {os.environ.get('JAVA_HOME', '未设置')}")

    # 步骤 1: 分析所有测试（生成 all_tests_jar.csv）
    if not Path(all_tests_csv).exists():
        print(f"\n[步骤 1/3] 分析所有测试 -> {all_tests_csv}")
        args = [
            '--projects', 'all_repos.txt',
            '--root', '/data/xuhaoran/github',
            '--output', all_tests_csv,
            '--jar', 'maven-test-metrics-1.0-SNAPSHOT.jar',
        ]
        with patch('sys.argv', ['maven_test_metrics_jar.py'] + args):
            maven_test_metrics_jar.main()
    else:
        print(f"\n[步骤 1/3] 跳过（已存在）: {all_tests_csv}")

    # 步骤 2: 激进方案筛选
    print(f"\n[步骤 2/3] 激进方案筛选: called_packages_count >= {min_packages}, exclude_mock={exclude_mock}")
    select_complex_integration_tests.select_tests(
        input_csv=Path(all_tests_csv),
        output_csv=Path(selected_csv),
        min_packages=min_packages,
        exclude_mock=exclude_mock,
    )

    # 步骤 3: 项目级采样
    print(f"\n[步骤 3/3] 项目级采样: 每项目最多 {max_per_project} 条")
    sample_by_project.sample_by_project(
        input_csv=Path(selected_csv),
        output_csv=Path(benchmark_csv),
        max_per_project=max_per_project,
    )

    print(f"\n{'='*60}")
    print("Benchmark 构建完成！")
    print(f"  原始数据: {all_tests_csv}")
    print(f"  筛选结果: {selected_csv}")
    print(f"  最终采样: {benchmark_csv}")
    print(f"{'='*60}")


if __name__ == '__main__':
    args = [
        '--csv', 'integration_benchmark_v7_n5.csv',
        '--root', '/data/xuhaoran/github',
        '--output', 'integration_benchmark_v7_n5',
        '--mode', 'all'
    ]
    with patch('sys.argv', ['main.py'] + args):
        extract_test_snippets.main()
