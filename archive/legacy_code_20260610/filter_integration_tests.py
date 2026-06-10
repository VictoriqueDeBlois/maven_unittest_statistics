#!/usr/bin/env python3
"""
集成测试筛选工具

从CSV文件中筛选出集成测试相关的测试用例。
集成测试通常在类名或方法名中包含特定关键词。
"""

import csv
import re
from pathlib import Path
from typing import List, Set, Tuple
import argparse
import logging

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# 集成测试常见关键词（按优先级分组）
INTEGRATION_TEST_KEYWORDS = {
    # 高优先级：明确的集成测试标识
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

    # 中优先级：常见后缀缩写（需精确匹配）
    'IT',   # IntegrationTest
    'E2E',  # End-to-End
    'ST',   # SystemTest
    'AT',   # AcceptanceTest
    'CT',   # ContractTest
    'FT',   # FunctionalTest
}

# 需要排除的关键词（单元测试等）
UNIT_TEST_KEYWORDS = {
    'unit', 'unittest', 'units',
    'UT',
}


def tokenize_camel_case(name: str) -> List[str]:
    """
    将驼峰命名和下划线命名的字符串分词
    例如: OrderFlowIT -> ['Order', 'Flow', 'IT']
         integration_test -> ['integration', 'test']
         ApiE2ETest -> ['Api', 'E2E', 'Test']
    """
    # 先按下划线分词
    parts = name.replace('-', '_').split('_')
    
    tokens = []
    for part in parts:
        if not part:
            continue
        # 驼峰分词：在大写字母前分割，但保持连续的大写字母+数字组合
        # 例如: E2E, IT, API 保持完整
        camel_tokens = re.findall(r'[A-Z]+[0-9]*[A-Z]*(?=[A-Z][a-z]|$)|[A-Z]?[a-z]+', part)
        tokens.extend(camel_tokens)
    
    return tokens


def is_integration_test(test_full_name: str,
                        keywords: Set[str] = INTEGRATION_TEST_KEYWORDS,
                        exclude_keywords: Set[str] = UNIT_TEST_KEYWORDS,
                        case_sensitive: bool = False) -> Tuple[bool, str | None]:
    """
    判断是否为集成测试（改进版：使用分词匹配）

    Args:
        test_full_name: 测试用例全名，格式: com.example.MyTest#testMethod
        keywords: 集成测试关键词集合
        exclude_keywords: 需要排除的关键词集合（如单元测试）
        case_sensitive: 是否区分大小写

    Returns:
        (是否为集成测试, 命中的关键词)，如果未命中则关键词为 None
    """
    # 分离类名和方法名
    if '#' not in test_full_name:
        logger.warning(f"Invalid test name format: {test_full_name}")
        return False, None

    full_class_name, method_name = test_full_name.split('#', 1)

    # 获取简单类名（不含包名）
    class_name = full_class_name.split('.')[-1]

    # 对类名和方法名进行分词
    class_tokens = tokenize_camel_case(class_name)
    method_tokens = tokenize_camel_case(method_name)
    all_tokens = class_tokens + method_tokens

    # 构建小写版本的tokens用于不区分大小写的匹配
    all_tokens_lower = [t.lower() for t in all_tokens]
    class_tokens_lower = [t.lower() for t in class_tokens]
    class_name_lower = class_name.lower()
    method_name_lower = method_name.lower()

    # 构建关键词查找字典（小写->原始）
    keyword_map = {kw.lower(): kw for kw in keywords}
    exclude_keyword_map = {kw.lower(): kw for kw in exclude_keywords}

    # 1. 首先检查是否包含排除关键词
    if not case_sensitive:
        check_tokens = all_tokens_lower
    else:
        check_tokens = all_tokens

    for token in check_tokens:
        token_lower = token.lower()
        # 精确匹配排除关键词
        if token_lower in exclude_keyword_map:
            return False, None
        # 也检查整个类名（对于一些特殊情况）
        if not case_sensitive and class_name.lower() == token_lower:
            if token_lower in exclude_keyword_map:
                return False, None

    # 2. 检查是否包含集成测试关键词
    matched_keyword = None

    # 策略1：分词后的精确匹配（优先）
    for token_lower in all_tokens_lower:
        if token_lower in keyword_map:
            matched_keyword = keyword_map[token_lower]
            return True, matched_keyword

    # 策略2：对于大写缩写（IT, E2E等），检查是否作为驼峰命名的独立部分
    # 例如: OrderFlowIT, PaymentE2ETest
    if not case_sensitive:
        # 检查原始类名中的大写字母+数字序列
        # 需要智能分割：E2ETest -> E2E, ITTest -> IT
        uppercase_sequences = re.findall(r'[A-Z]+[0-9]*[A-Z]*', class_name)
        for seq in uppercase_sequences:
            # 尝试不同的分割方式
            if seq in keyword_map:
                matched_keyword = keyword_map[seq]
                return True, matched_keyword
            # 去除末尾的 Test/TestS 等
            if seq.endswith('TEST'):
                seq_without_test = seq[:-4]
                if seq_without_test in keyword_map:
                    matched_keyword = keyword_map[seq_without_test]
                    return True, matched_keyword
            elif seq.endswith('TESTS'):
                seq_without_test = seq[:-5]
                if seq_without_test in keyword_map:
                    matched_keyword = keyword_map[seq_without_test]
                    return True, matched_keyword

    # 策略3：检查连字符或下划线连接的关键词
    # 例如: end-to-end, end_to_end
    class_name_normalized = class_name.lower().replace('-', '_')
    for keyword_lower, orig_kw in keyword_map.items():
        if '_' in keyword_lower or '-' in keyword_lower:
            keyword_normalized = keyword_lower.replace('-', '_')
            if keyword_normalized in class_name_normalized:
                matched_keyword = orig_kw
                return True, matched_keyword

    # 策略4：检查常见测试类命名模式
    # 例如: XXXIntegrationTest, XXXE2ETest
    for keyword_lower, orig_kw in keyword_map.items():
        if len(keyword_lower) >= 4:  # 只对长关键词进行前缀/后缀匹配
            # 作为前缀: IntegrationTest, E2ETest
            if class_name_lower.startswith(keyword_lower):
                matched_keyword = orig_kw
                return True, matched_keyword
            # 作为后缀: TestIntegration, TestE2E
            if class_name_lower.endswith(keyword_lower):
                matched_keyword = orig_kw
                return True, matched_keyword
            # 包含Test的模式: XXXIntegrationTest, XXXE2ETest
            if 'test' in class_name_lower:
                # 移除Test后缀后再检查
                without_test = class_name_lower.replace('test', '')
                if keyword_lower in without_test:
                    matched_keyword = orig_kw
                    return True, matched_keyword

    return False, None


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

    # 添加新列到 fieldnames
    output_fieldnames = list(fieldnames) if fieldnames else []
    if 'matched_keyword' not in output_fieldnames:
        output_fieldnames.append('matched_keyword')

    for row in test_cases:
        test_name = row.get('test_full_name', '')
        is_integration, matched_kw = is_integration_test(
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
            # 创建新行，添加 matched_keyword 列
            new_row = dict(row)
            new_row['matched_keyword'] = matched_kw if is_integration else 'N/A'
            filtered_cases.append(new_row)

    logger.info(f"Integration tests: {integration_count}")
    logger.info(f"Unit/Other tests: {unit_count}")
    logger.info(f"Filtered result: {len(filtered_cases)} test cases")

    # 写入输出CSV
    if filtered_cases:
        try:
            with open(output_csv, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=output_fieldnames)
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

        is_int, matched_kw = is_integration_test(test_name, keywords=keywords, exclude_keywords=exclude_kws,
                               case_sensitive=case_sensitive)
        if is_int:
            integration_tests.append(row)

            # 统计命中的关键词
            if matched_kw and matched_kw in keyword_count:
                keyword_count[matched_kw] += 1
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