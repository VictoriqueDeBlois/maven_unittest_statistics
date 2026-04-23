#!/usr/bin/env python3
"""
项目级采样脚本

对已有筛选结果按项目分组，每个项目取 Top-N 条（按 called_packages_count 降序，
同分按 called_methods_count 降序），保证项目多样性。
"""

import csv
import sys
from pathlib import Path
from collections import Counter


def sample_by_project(
    input_csv: Path,
    output_csv: Path,
    max_per_project: int = 10,
):
    """
    项目级采样。

    Args:
        input_csv: 输入CSV（如 selected_integration_tests_v7.csv）
        output_csv: 输出CSV
        max_per_project: 每个项目最多保留的条数
    """
    print(f"读取: {input_csv}")

    rows = []
    with open(input_csv, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames) if reader.fieldnames else []
        for row in reader:
            rows.append(row)

    print(f"输入记录数: {len(rows):,}")
    print(f"输入项目数: {len(set(r['project_name'] for r in rows))}")
    print(f"每项目上限: {max_per_project}")

    # 按项目分组，组内排序
    project_groups = {}
    for row in rows:
        proj = row['project_name']
        if proj not in project_groups:
            project_groups[proj] = []
        project_groups[proj].append(row)

    # 排序规则：called_packages_count 降序 -> called_methods_count 降序
    def sort_key(row):
        pkg = int(row.get('called_packages_count', 0))
        meth = int(row.get('called_methods_count', 0))
        # 也可以考虑 setup_length 作为第三排序键
        setup = int(row.get('setup_length', 0))
        return (-pkg, -meth, -setup)

    sampled = []
    for proj, group in project_groups.items():
        group.sort(key=sort_key)
        sampled.extend(group[:max_per_project])

    # 写入
    with open(output_csv, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in sampled:
            writer.writerow(row)

    # 统计
    out_projects = set(r['project_name'] for r in sampled)
    proj_counts = Counter(r['project_name'] for r in sampled)

    print("\n" + "=" * 60)
    print("采样结果统计")
    print("=" * 60)
    print(f"输出记录数: {len(sampled):,}")
    print(f"输出项目数: {len(out_projects)}")
    print(f"每项目平均: {len(sampled)/len(out_projects):.1f} 条")

    print(f"\n项目分布:")
    print(f"  取满 {max_per_project} 条的项目: {sum(1 for c in proj_counts.values() if c >= max_per_project)}")
    print(f"  未满 {max_per_project} 条的项目: {sum(1 for c in proj_counts.values() if c < max_per_project)}")

    print(f"\nTop 10 项目:")
    for p, c in proj_counts.most_common(10):
        print(f"  {p}: {c}")

    print(f"\n输出文件: {output_csv}")
    return len(sampled)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="项目级采样")
    parser.add_argument('--input', '-i', required=True, help='输入CSV')
    parser.add_argument('--output', '-o', required=True, help='输出CSV')
    parser.add_argument('--max-per-project', '-n', type=int, default=10,
                        help='每个项目最多保留条数（默认10）')
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"错误: 找不到输入文件 {input_path}")
        sys.exit(1)

    sample_by_project(
        input_csv=input_path,
        output_csv=Path(args.output),
        max_per_project=args.max_per_project,
    )


if __name__ == '__main__':
    main()
