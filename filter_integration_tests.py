#!/usr/bin/env python3
"""
集成测试筛选工具

从CSV文件中筛选出集成测试相关的测试用例。
集成测试通常在类名或方法名中包含特定关键词。
"""

import csv
import re
from pathlib import Path
from typing import List, Set
import argparse
import logging

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# 集成测试常见关键词
INTEGRATION_TEST_KEYWORDS = {
    # 英文关键词
    'integration',
    'integrated', 'integ', 'it',

    'e2e', 'end2end', 'endtoend',
    'system', 'functional',
    'acceptance', 'contract',
    'component', 'scenario',
    'smoke', 'sanity',

    # 缩写
    'IT',  # IntegrationTest
    'E2E', # End-to-End
    'ST',  # SystemTest
    'AT',  # AcceptanceTest
    'CT',  # ContractTest
    'FT',  # FunctionalTest
}

# 单元测试关键词（用于排除）
UNIT_TEST_KEYWORDS = {
    'unit', 'unittest', 'ut', 'UT'
}


def is_integration_test(test_full_name: str,
                        keywords: Set[str] = INTEGRATION_TEST_KEYWORDS,
                        exclude_keywords: Set[str] = UNIT_TEST_KEYWORDS,
                        case_sensitive: bool = False) -> bool:
    """
    判断是否为集成测试

    Args:
        test_full_name: 测试用例全名，格式: com.example.MyTest#testMethod
        keywords: 集成测试关键词集合
        exclude_keywords: 需要排除的关键词集合（如单元测试）
        case_sensitive: 是否区分大小写

    Returns:
        是否为集成测试
    """
    # 分离类名和方法名
    if '#' not in test_full_name:
        logger.warning(f"Invalid test name format: {test_full_name}")
        return False

    full_class_name, method_name = test_full_name.split('#', 1)

    # 获取简单类名（不含包名）
    class_name = full_class_name.split('.')[-1]

    # 组合类名和方法名用于匹配
    combined_name = f"{class_name}#{method_name}"

    if not case_sensitive:
        combined_name_lower = combined_name.lower()
        keywords_lower = {k.lower() for k in keywords}
        exclude_lower = {k.lower() for k in exclude_keywords}
    else:
        combined_name_lower = combined_name
        keywords_lower = keywords
        exclude_lower = exclude_keywords

    # 首先检查是否包含排除关键词
    for exclude_kw in exclude_lower:
        # 对于排除关键词，使用单词边界确保精确匹配
        pattern = r'\b' + re.escape(exclude_kw) + r'\b'
        if re.search(pattern, combined_name_lower, re.IGNORECASE if not case_sensitive else 0):
            return False

    # 检查是否包含集成测试关键词
    for keyword in keywords_lower:
        # 对于短关键词（<=3字符），需要特殊处理
        if len(keyword) <= 3:
            # 如果原始关键词是全大写（如IT, E2E），在原始名称中检查
            original_keyword = None
            for orig_kw in keywords:
                if orig_kw.lower() == keyword:
                    original_keyword = orig_kw
                    break

            if original_keyword and original_keyword.isupper():
                # 全大写缩写，检查原始字符串（区分大小写）
                # 例如 OrderFlowIT, UserE2ETest
                if original_keyword in (class_name + '#' + method_name):
                    return True

            # 使用单词边界匹配
            pattern = r'\b' + re.escape(keyword) + r'\b'
            if re.search(pattern, combined_name_lower, re.IGNORECASE if not case_sensitive else 0):
                return True
        else:
            # 对于长关键词，使用包含匹配
            if keyword in combined_name_lower:
                return True

    return False


def filter_integration_tests(
        input_csv: Path,
        output_csv: Path,
        custom_keywords: List[str] = None,
        exclude_keywords: List[str] = None,
        case_sensitive: bool = False,
        inverse: bool = False
) -> int:
    """
    从CSV中筛选集成测试

    Args:
        input_csv: 输入CSV文件
        output_csv: 输出CSV文件
        custom_keywords: 自定义关键词（会添加到默认关键词）
        exclude_keywords: 排除关键词（会添加到默认排除关键词）
        case_sensitive: 是否区分大小写
        inverse: 反向筛选（筛选出非集成测试）

    Returns:
        筛选出的测试用例数量
    """
    # 构建关键词集合
    keywords = INTEGRATION_TEST_KEYWORDS.copy()
    if custom_keywords:
        keywords.update(custom_keywords)

    exclude_kws = UNIT_TEST_KEYWORDS.copy()
    if exclude_keywords:
        exclude_kws.update(exclude_keywords)

    logger.info(f"Reading CSV: {input_csv}")
    logger.info(f"Integration keywords: {sorted(keywords)[:10]}...")
    logger.info(f"Exclude keywords: {sorted(exclude_kws)}")
    logger.info(f"Case sensitive: {case_sensitive}")
    logger.info(f"Inverse mode: {inverse}")

    # 读取CSV
    test_cases = []
    try:
        with open(input_csv, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            for row in reader:
                test_cases.append(row)
    except Exception as e:
        logger.error(f"Error reading CSV: {e}")
        return 0

    logger.info(f"Total test cases: {len(test_cases)}")

    # 筛选
    filtered_cases = []
    integration_count = 0
    unit_count = 0

    for row in test_cases:
        test_name = row.get('test_full_name', '')
        is_integration = is_integration_test(
            test_name,
            keywords=keywords,
            exclude_keywords=exclude_kws,
            case_sensitive=case_sensitive
        )

        if is_integration:
            integration_count += 1
        else:
            unit_count += 1

        # 根据模式决定是否保留
        if (is_integration and not inverse) or (not is_integration and inverse):
            filtered_cases.append(row)

    logger.info(f"Integration tests: {integration_count}")
    logger.info(f"Unit/Other tests: {unit_count}")
    logger.info(f"Filtered result: {len(filtered_cases)} test cases")

    # 写入输出CSV
    if filtered_cases:
        try:
            with open(output_csv, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for row in filtered_cases:
                    writer.writerow(row)

            logger.info(f"Results written to: {output_csv}")
        except Exception as e:
            logger.error(f"Error writing CSV: {e}")
            return 0
    else:
        logger.warning("No test cases matched the filter criteria")

    return len(filtered_cases)


def show_statistics(
        input_csv: Path,
        custom_keywords: List[str] = None,
        exclude_keywords: List[str] = None,
        case_sensitive: bool = False
):
    """
    显示集成测试统计信息

    Args:
        input_csv: 输入CSV文件
        custom_keywords: 自定义关键词
        exclude_keywords: 排除关键词
        case_sensitive: 是否区分大小写
    """
    # 构建关键词集合
    keywords = INTEGRATION_TEST_KEYWORDS.copy()
    if custom_keywords:
        keywords.update(custom_keywords)

    exclude_kws = UNIT_TEST_KEYWORDS.copy()
    if exclude_keywords:
        exclude_kws.update(exclude_keywords)

    # 读取CSV
    test_cases = []
    try:
        with open(input_csv, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                test_cases.append(row)
    except Exception as e:
        logger.error(f"Error reading CSV: {e}")
        return

    # 统计
    integration_tests = []
    unit_tests = []
    keyword_count = {kw: 0 for kw in keywords}

    for row in test_cases:
        test_name = row.get('test_full_name', '')

        if is_integration_test(test_name, keywords=keywords, exclude_keywords=exclude_kws,
                               case_sensitive=case_sensitive):
            integration_tests.append(row)

            # 统计命中的关键词
            combined = test_name.lower() if not case_sensitive else test_name
            for kw in keywords:
                kw_check = kw.lower() if not case_sensitive else kw
                # 使用简单的包含匹配
                if kw_check in combined:
                    keyword_count[kw] += 1
        else:
            unit_tests.append(row)

    # 显示统计
    print("\n" + "="*60)
    print("集成测试统计报告")
    print("="*60)
    print(f"\n总测试用例数: {len(test_cases)}")
    print(f"集成测试数量: {len(integration_tests)} ({len(integration_tests)/len(test_cases)*100:.1f}%)")
    print(f"单元测试数量: {len(unit_tests)} ({len(unit_tests)/len(test_cases)*100:.1f}%)")

    # 关键词命中统计
    print(f"\n关键词命中统计（Top 10）:")
    sorted_keywords = sorted(keyword_count.items(), key=lambda x: x[1], reverse=True)
    for kw, count in sorted_keywords[:10]:
        if count > 0:
            print(f"  {kw:20s}: {count:5d}")

    # 项目级别统计
    project_stats = {}
    for row in integration_tests:
        project = row.get('project_name', 'unknown')
        project_stats[project] = project_stats.get(project, 0) + 1

    if project_stats:
        print(f"\n按项目统计（Top 10）:")
        sorted_projects = sorted(project_stats.items(), key=lambda x: x[1], reverse=True)
        for project, count in sorted_projects[:10]:
            print(f"  {project:40s}: {count:5d}")

    # 示例
    print(f"\n集成测试示例（前5个）:")
    for i, row in enumerate(integration_tests[:5], 1):
        print(f"  {i}. {row['test_full_name']}")

    print("\n" + "="*60 + "\n")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description='筛选集成测试用例',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 筛选集成测试到新文件
  python3 filter_integration_tests.py --input test_metrics.csv --output integration_tests.csv
  
  # 添加自定义关键词
  python3 filter_integration_tests.py --input test_metrics.csv --output it_tests.csv \\
      --add-keywords "workflow" "orchestration"
  
  # 只显示统计信息，不生成文件
  python3 filter_integration_tests.py --input test_metrics.csv --stats-only
  
  # 反向筛选（筛选出单元测试）
  python3 filter_integration_tests.py --input test_metrics.csv --output unit_tests.csv --inverse
  
  # 区分大小写（严格匹配IT, E2E等缩写）
  python3 filter_integration_tests.py --input test_metrics.csv --output it_tests.csv --case-sensitive
        """
    )

    parser.add_argument('--input', '-i', required=True, help='输入CSV文件')
    parser.add_argument('--output', '-o', help='输出CSV文件')
    parser.add_argument('--add-keywords', nargs='+', help='添加自定义关键词')
    parser.add_argument('--exclude-keywords', nargs='+', help='添加排除关键词')
    parser.add_argument('--case-sensitive', action='store_true', help='区分大小写')
    parser.add_argument('--inverse', action='store_true', help='反向筛选（筛选出非集成测试）')
    parser.add_argument('--stats-only', action='store_true', help='只显示统计信息，不生成文件')

    args = parser.parse_args()

    input_csv = Path(args.input)

    if not input_csv.exists():
        logger.error(f"Input file not found: {input_csv}")
        return 1

    # 统计模式
    if args.stats_only:
        show_statistics(
            input_csv,
            custom_keywords=args.add_keywords,
            exclude_keywords=args.exclude_keywords,
            case_sensitive=args.case_sensitive
        )
        return 0

    # 筛选模式
    if not args.output:
        logger.error("--output is required when not in --stats-only mode")
        return 1

    output_csv = Path(args.output)

    count = filter_integration_tests(
        input_csv,
        output_csv,
        custom_keywords=args.add_keywords,
        exclude_keywords=args.exclude_keywords,
        case_sensitive=args.case_sensitive,
        inverse=args.inverse
    )

    if count > 0:
        logger.info(f"Successfully filtered {count} test cases")
        return 0
    else:
        logger.warning("No test cases matched")
        return 1


if __name__ == '__main__':
    exit(main())