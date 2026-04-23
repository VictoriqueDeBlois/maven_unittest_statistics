#!/usr/bin/env python3
"""
复杂集成测试筛选脚本（激进方案）

筛选条件：
  1. called_packages_count >= 5
  2. uses_mock == false

附加功能：
  配合 filter_integration_tests.py 的关键词匹配，
  在输出 CSV 中附加 matched_keyword 列（未命中则为空），
  但关键词匹配结果不影响筛选（仅作为观察标注）。
"""

import csv
import re
import sys
from pathlib import Path
from typing import List, Set, Tuple

# ── 从 filter_integration_tests.py 复用的关键词配置 ────────────────────────────
INTEGRATION_TEST_KEYWORDS = {
    'integration', 'integrations', 'integrationtest',
    'e2e', 'end2end', 'end-to-end', 'endtoend',
    'systemtest', 'system',
    'acceptancetest', 'acceptance',
    'contracttest', 'contract',
    'componenttest', 'component',
    'functionaltest', 'functional',
    'scenariotest', 'scenario',
    'smoketest', 'smoke',
    'sanitytest', 'sanity',
    'IT', 'E2E', 'ST', 'AT', 'CT', 'FT',
}

UNIT_TEST_KEYWORDS = {'unit', 'unittest', 'units', 'UT'}


def tokenize_camel_case(name: str) -> List[str]:
    """驼峰分词"""
    parts = name.replace('-', '_').split('_')
    tokens = []
    for part in parts:
        if not part:
            continue
        camel_tokens = re.findall(r'[A-Z]+[0-9]*[A-Z]*(?=[A-Z][a-z]|$)|[A-Z]?[a-z]+', part)
        tokens.extend(camel_tokens)
    return tokens


def match_integration_keyword(test_full_name: str) -> Tuple[bool, str | None]:
    """
    检查测试名是否命中集成测试关键词。
    返回 (是否命中, 命中的关键词)。
    """
    if '#' not in test_full_name:
        return False, None

    full_class_name, method_name = test_full_name.split('#', 1)
    class_name = full_class_name.split('.')[-1]

    class_tokens = tokenize_camel_case(class_name)
    method_tokens = tokenize_camel_case(method_name)
    all_tokens = class_tokens + method_tokens
    all_tokens_lower = [t.lower() for t in all_tokens]
    class_name_lower = class_name.lower()

    keyword_map = {kw.lower(): kw for kw in INTEGRATION_TEST_KEYWORDS}
    exclude_keyword_map = {kw.lower(): kw for kw in UNIT_TEST_KEYWORDS}

    # 1. 排除关键词
    for token_lower in all_tokens_lower:
        if token_lower in exclude_keyword_map:
            return False, None

    # 2. 分词精确匹配
    for token_lower in all_tokens_lower:
        if token_lower in keyword_map:
            return True, keyword_map[token_lower]

    # 3. 大写缩写匹配（IT, E2E 等）
    uppercase_sequences = re.findall(r'[A-Z]+[0-9]*[A-Z]*', class_name)
    for seq in uppercase_sequences:
        if seq in keyword_map:
            return True, keyword_map[seq]
        for suffix, length in [('TEST', 4), ('TESTS', 5)]:
            if seq.endswith(suffix):
                seq_without = seq[:-length]
                if seq_without in keyword_map:
                    return True, keyword_map[seq_without]

    # 4. 连字符/下划线连接的关键词
    class_name_normalized = class_name.lower().replace('-', '_')
    for keyword_lower, orig_kw in keyword_map.items():
        if '_' in keyword_lower or '-' in keyword_lower:
            keyword_normalized = keyword_lower.replace('-', '_')
            if keyword_normalized in class_name_normalized:
                return True, orig_kw

    # 5. 前缀/后缀/包含匹配
    for keyword_lower, orig_kw in keyword_map.items():
        if len(keyword_lower) >= 4:
            if class_name_lower.startswith(keyword_lower):
                return True, orig_kw
            if class_name_lower.endswith(keyword_lower):
                return True, orig_kw
            if 'test' in class_name_lower:
                without_test = class_name_lower.replace('test', '')
                if keyword_lower in without_test:
                    return True, orig_kw

    return False, None


def select_tests(
    input_csv: Path,
    output_csv: Path,
    min_packages: int = 5,
    exclude_mock: bool = True,
):
    """
    主筛选逻辑。
    """
    print(f"读取: {input_csv}")

    rows = []
    with open(input_csv, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames) if reader.fieldnames else []
        for row in reader:
            rows.append(row)

    print(f"总记录数: {len(rows):,}")

    # 追加关键词列
    extra_fields = ['has_keyword', 'matched_keyword']
    output_fieldnames = fieldnames + [f for f in extra_fields if f not in fieldnames]

    selected = []
    keyword_hit_count = 0
    no_keyword_count = 0
    mock_excluded = 0
    pkg_excluded = 0

    for row in rows:
        pkg_count = int(row.get('called_packages_count', 0))
        uses_mock = row.get('uses_mock', '').strip().lower() == 'true'
        test_name = row.get('test_full_name', '')

        # 条件1: called_packages_count
        if pkg_count < min_packages:
            pkg_excluded += 1
            continue

        # 条件2: no mock
        if exclude_mock and uses_mock:
            mock_excluded += 1
            continue

        # 关键词匹配（仅标注，不影响筛选）
        is_hit, matched_kw = match_integration_keyword(test_name)
        if is_hit:
            keyword_hit_count += 1
        else:
            no_keyword_count += 1

        new_row = dict(row)
        new_row['has_keyword'] = 'true' if is_hit else 'false'
        new_row['matched_keyword'] = matched_kw if is_hit else ''
        selected.append(new_row)

    # 写入
    with open(output_csv, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=output_fieldnames)
        writer.writeheader()
        for row in selected:
            writer.writerow(row)

    # 统计输出
    print("\n" + "=" * 60)
    print("筛选结果统计")
    print("=" * 60)
    print(f"\n输入总记录:     {len(rows):,}")
    print(f"called_packages_count < {min_packages}: 排除 {pkg_excluded:,}")
    print(f"uses_mock = true:              排除 {mock_excluded:,}")
    print(f"最终选中:                    {len(selected):,}")
    print(f"\n其中命中关键词: {keyword_hit_count:,} ({keyword_hit_count/len(selected)*100:.1f}%)")
    print(f"其中未命中关键词: {no_keyword_count:,} ({no_keyword_count/len(selected)*100:.1f}%)")

    projects = sorted(set(r['project_name'] for r in selected))
    print(f"\n覆盖项目数: {len(projects)}")
    print(f"每项目平均: {len(selected)/len(projects):.1f} 条")

    print(f"\n输出文件: {output_csv}")

    return len(selected)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="复杂集成测试筛选（激进方案）")
    parser.add_argument('--input', '-i', default='all_tests_jar.csv', help='输入CSV')
    parser.add_argument('--output', '-o', default='selected_integration_tests.csv', help='输出CSV')
    parser.add_argument('--min-packages', '-p', type=int, default=5, help='called_packages_count 阈值（默认5）')
    parser.add_argument('--keep-mock', action='store_true', help='保留使用mock的测试')
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"错误: 找不到输入文件 {input_path}")
        sys.exit(1)

    select_tests(
        input_csv=input_path,
        output_csv=Path(args.output),
        min_packages=args.min_packages,
        exclude_mock=not args.keep_mock,
    )


if __name__ == '__main__':
    main()
