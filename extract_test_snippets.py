#!/usr/bin/env python3
"""
测试用例代码片段提取工具（多进程版）

从CSV文件中读取测试用例信息，提取对应的源代码片段到单独的文件中。
支持两种提取方式：
1. Python 正则提取（默认）
2. 通过 maven-test-metrics jar 的 --debug 模式提取
"""

import argparse
import csv
import logging
import os
import re
import subprocess
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from tqdm import tqdm

# 日志统一写到 logs/ 目录，避免散落在项目根目录。
_LOG_FILE = Path("logs") / f"{Path(__file__).stem}.log"


def _setup_logger() -> logging.Logger:
    """配置日志输出到文件，避免控制台输出。"""
    log = logging.getLogger(__name__)
    if not log.handlers:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(_LOG_FILE, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        # 阻止日志向上传播到 root logger，防止在控制台重复输出
        log.propagate = False
    return log


logger = _setup_logger()


def sanitize_filename(name: str) -> str:
    """
    清理文件名，移除或替换非法字符
    """
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = name.replace('#', '__')
    name = name.replace(' ', '_')
    return name


def extract_method_code(file_path: Path, class_name: str, method_name: str) -> Optional[str]:
    """
    从Java文件中提取指定方法的代码（Python 正则方式）
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        pattern = rf'(@\w+\s*(?:\([^)]*\))?\s*)*\s*(?:public|private|protected)?\s+(?:static\s+)?(?:\w+\s+)?{re.escape(method_name)}\s*\([^)]*\)\s*(?:throws\s+\w+(?:,\s*\w+)*)?\s*\{{'

        match = re.search(pattern, content)
        if not match:
            return None

        method_start = match.start()
        brace_start = match.end() - 1

        brace_count = 1
        pos = brace_start + 1

        while pos < len(content) and brace_count > 0:
            if content[pos] == '{':
                brace_count += 1
            elif content[pos] == '}':
                brace_count -= 1
            pos += 1

        if brace_count == 0:
            return content[method_start:pos]
        return None

    except Exception:
        return None


def _run_jar_extract(
    jar_path: Path,
    project_path: Path,
    project_name: str,
    file_arg: str,
    method_name: str,
    debug_output: Path,
    format: str = "annotated",
) -> bool:
    """调用 jar 的 --debug 模式，返回是否成功。"""
    # 优先使用 JAVA_HOME 下的 java
    java_home = os.environ.get('JAVA_HOME', '')
    java_bin = os.path.join(java_home, 'bin', 'java') if java_home else 'java'
    if java_home and not os.path.exists(java_bin):
        java_bin = 'java'

    cmd = [
        java_bin, '-jar', str(jar_path),
        '--debug',
        '--format', format,
        '--root', str(project_path),
        '--name', project_name,
        '--file', file_arg,
        '--method', method_name,
        '--debug-output', str(debug_output),
    ]
    logger.info(f"[jar] cmd: {' '.join(cmd)}")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
        encoding='utf-8',
    )

    if result.returncode != 0:
        logger.warning(
            f"[jar] exit_code={result.returncode}, file_arg={file_arg}, "
            f"stderr={result.stderr.strip()[:500]}, stdout={result.stdout.strip()[:500]}"
        )
        return False

    if not debug_output.exists():
        logger.warning(f"[jar] debug-output not found: {debug_output} (file_arg={file_arg})")
        return False

    if not debug_output.read_text(encoding='utf-8').strip():
        logger.warning(f"[jar] debug-output is empty: {debug_output} (file_arg={file_arg})")
        return False

    return True


def extract_method_code_via_jar(
    jar_path: Path,
    project_path: Path,
    project_name: str,
    test_file: Path,
    method_name: str,
    debug_output_path: Path,
    format: str = "annotated",
) -> Optional[str]:
    """
    通过 maven-test-metrics jar 的 --debug 模式提取方法代码。

    调用命令格式：
        java -jar <jar> --debug --root <project> --name <project_name>
              --file <Test.java> --method <method_name>
              --debug-output <debug_output_path>

    兼容性处理：jar 对 --file 的解析可能支持带后缀或不带后缀，
    因此先尝试 test_file.name，失败后再尝试 test_file.stem。
    """
    # 尝试 1: 完整绝对路径（带 .java 后缀）
    if _run_jar_extract(
        jar_path, project_path, project_name,
        str(test_file), method_name, debug_output_path, format
    ):
        return debug_output_path.read_text(encoding='utf-8')

    # 尝试 2: 完整绝对路径（不带 .java 后缀）
    if _run_jar_extract(
        jar_path, project_path, project_name,
        str(test_file.with_suffix('')), method_name, debug_output_path, format
    ):
        return debug_output_path.read_text(encoding='utf-8')

    return None


def find_test_file(projects_root: Path, project_name: str, full_class_name: str) -> Optional[Path]:
    """
    根据项目名和完整类名查找测试文件
    """
    project_path = projects_root / project_name

    if not project_path.exists():
        return None

    class_path = full_class_name.replace('.', '/') + '.java'

    for java_file in project_path.rglob('*.java'):
        if str(java_file).endswith(class_path.replace('/', os.sep)):
            path_str = str(java_file)
            if '/test/' in path_str or '\\test\\' in path_str:
                return java_file

    return None


def build_header(csv_row: Dict[str, str]) -> str:
    """
    根据CSV行数据动态生成文件头注释。
    遍历所有列，优先输出 project_name 和 test_full_name，其余随后。
    """
    preferred_order = ['project_name', 'test_full_name']
    lines = []

    # 优先输出 repo 名和测试全名
    for key in preferred_order:
        if key in csv_row:
            lines.append(f"// {key}: {csv_row[key]}")

    # 再输出其余字段
    for key, value in csv_row.items():
        if key not in preferred_order:
            lines.append(f"// {key}: {value}")

    if lines:
        return "\n".join(lines) + "\n\n"
    return "\n"


def extract_test_case_code(
    csv_row: Dict[str, str],
    projects_root: Path,
    output_dir: Path,
    jar_path: Optional[Path] = None,
    jar_format: str = "annotated",
) -> Tuple[bool, str]:
    """
    提取单个测试用例的代码并保存到文件。
    返回 (是否成功, 信息消息)
    """
    project_name = csv_row.get('project_name', '')
    test_full_name = csv_row.get('test_full_name', '')

    if '#' not in test_full_name:
        return False, f"Invalid test name format: {test_full_name}"

    full_class_name, method_name = test_full_name.split('#', 1)
    class_name = full_class_name.split('.')[-1]

    # 查找测试文件
    test_file = find_test_file(projects_root, project_name, full_class_name)
    if not test_file:
        return False, f"Test file not found: {test_full_name}"

    # 生成多级目录结构
    project_parts = project_name.split('/')
    if len(project_parts) == 2:
        owner, repo = project_parts
    else:
        owner = 'unknown'
        repo = sanitize_filename(project_name.replace('/', '_'))

    safe_class_name = sanitize_filename(class_name)
    safe_method_name = sanitize_filename(method_name)

    output_dir_structure = output_dir / owner / repo / safe_class_name
    output_file = output_dir_structure / f"{safe_method_name}.java"
    txt_output_file = output_dir_structure / f"{safe_method_name}.txt"

    # 提取方法代码
    if jar_path and jar_path.exists():
        project_path = projects_root / project_name
        output_dir_structure.mkdir(parents=True, exist_ok=True)

        if jar_format == "raw-java":
            # raw-java 模式：jar 直接生成完整的 .java 文件（含 import、字段、调用链）
            method_code = extract_method_code_via_jar(
                jar_path, project_path, project_name, test_file, method_name, output_file, format="raw-java"
            )
            if not method_code:
                return False, f"Method code not found: {test_full_name}"
            return True, f"Extracted: {output_file.relative_to(output_dir)}"
        else:
            # annotated 模式：生成带标注的 .txt 文件
            method_code = extract_method_code_via_jar(
                jar_path, project_path, project_name, test_file, method_name, txt_output_file, format="annotated"
            )
            if not method_code:
                return False, f"Method code not found: {test_full_name}"
            try:
                header = build_header(csv_row)
                with open(txt_output_file, 'w', encoding='utf-8') as f:
                    f.write(header)
                    f.write(method_code)
                return True, f"Extracted: {txt_output_file.relative_to(output_dir)}"
            except Exception as e:
                return False, f"Error saving code to {txt_output_file}: {e}"
    else:
        method_code = extract_method_code(test_file, class_name, method_name)
        if not method_code:
            return False, f"Method code not found: {test_full_name}"

        # Python 正则模式：生成 .java 文件
        try:
            output_dir_structure.mkdir(parents=True, exist_ok=True)
            header = build_header(csv_row)
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(header)
                f.write(method_code)
            return True, f"Extracted: {output_file.relative_to(output_dir)}"
        except Exception as e:
            return False, f"Error saving code to {output_file}: {e}"


def _worker_extract(args: Tuple[Dict[str, str], Path, Path, Optional[Path], str]) -> Tuple[bool, str]:
    """多进程 worker 包装函数（必须是模块级函数才能被 pickle）。"""
    # 子进程需要独立初始化文件日志
    _setup_logger()
    csv_row, projects_root, output_dir, jar_path, jar_format = args
    return extract_test_case_code(csv_row, projects_root, output_dir, jar_path, jar_format)


def _read_csv_rows(csv_file: Path) -> List[Dict[str, str]]:
    """读取CSV文件并返回所有行。"""
    rows = []
    try:
        with open(csv_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
    except Exception as e:
        logger.error(f"Error reading CSV file: {e}")
    return rows


def _extract_cases_parallel(
    test_cases: List[Dict[str, str]],
    projects_root: Path,
    output_dir: Path,
    jar_path: Optional[Path],
    workers: int,
    desc: str,
    jar_format: str = "annotated",
) -> int:
    """通用并行提取逻辑。"""
    if not test_cases:
        logger.info("No test cases to extract.")
        return 0

    logger.info(f"Extracting {len(test_cases)} test cases using {workers} workers")

    args_list = [
        (tc, projects_root, output_dir, jar_path, jar_format)
        for tc in test_cases
    ]

    success_count = 0
    with ProcessPoolExecutor(max_workers=workers) as executor:
        future_to_case = {
            executor.submit(_worker_extract, args): args[0]
            for args in args_list
        }

        for future in tqdm(as_completed(future_to_case), total=len(future_to_case), desc=desc):
            test_case = future_to_case[future]
            try:
                success, msg = future.result()
            except Exception as e:
                success = False
                msg = f"Exception: {e}"

            if success:
                success_count += 1
                logger.info(f"OK  : {test_case.get('test_full_name', '')} -> {msg}")
            else:
                logger.warning(f"FAIL: {test_case.get('test_full_name', '')} -> {msg}")

    logger.info(f"Successfully extracted {success_count}/{len(test_cases)} test cases")
    return success_count


def extract_top_n_test_cases(
    csv_file: Path,
    projects_root: Path,
    output_dir: Path,
    top_n: int = 100,
    sort_by: str = 'oracle_length',
    jar_path: Optional[Path] = None,
    workers: int = 4,
    jar_format: str = "annotated",
) -> int:
    """
    提取CSV中指标最大的前N个测试用例的代码（多进程）。
    """
    logger.info(f"Reading CSV file: {csv_file}")
    test_cases = _read_csv_rows(csv_file)
    logger.info(f"Found {len(test_cases)} test cases in CSV")

    if sort_by not in ['oracle_length', 'assertion_count', 'mock_verify_count']:
        logger.warning(f"Invalid sort_by field: {sort_by}, using 'oracle_length'")
        sort_by = 'oracle_length'

    for tc in test_cases:
        try:
            tc[f'{sort_by}_int'] = int(tc.get(sort_by, 0))
        except (ValueError, TypeError):
            tc[f'{sort_by}_int'] = 0

    test_cases.sort(key=lambda x: x[f'{sort_by}_int'], reverse=True)
    top_cases = test_cases[:top_n]
    logger.info(f"Extracting top {len(top_cases)} test cases by {sort_by}")

    return _extract_cases_parallel(
        top_cases, projects_root, output_dir, jar_path, workers, desc="Top-N extraction", jar_format=jar_format
    )


def extract_specific_test_cases(
    csv_file: Path,
    projects_root: Path,
    output_dir: Path,
    test_names: List[str],
    jar_path: Optional[Path] = None,
    workers: int = 4,
    jar_format: str = "annotated",
) -> int:
    """
    提取指定的测试用例代码（多进程）。
    """
    logger.info(f"Reading CSV file: {csv_file}")
    test_cases_map = {}
    try:
        with open(csv_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                test_cases_map[row['test_full_name']] = row
    except Exception as e:
        logger.error(f"Error reading CSV file: {e}")
        return 0

    test_cases = []
    for test_name in test_names:
        if test_name not in test_cases_map:
            logger.warning(f"Test case not found in CSV: {test_name}")
            continue
        test_cases.append(test_cases_map[test_name])

    return _extract_cases_parallel(
        test_cases, projects_root, output_dir, jar_path, workers, desc="Specific extraction", jar_format=jar_format
    )


def extract_all_test_cases(
    csv_file: Path,
    projects_root: Path,
    output_dir: Path,
    jar_path: Optional[Path] = None,
    workers: int = 4,
    jar_format: str = "annotated",
) -> int:
    """
    提取CSV中的所有测试用例代码（多进程）。
    """
    logger.info(f"Reading CSV file: {csv_file}")
    test_cases = _read_csv_rows(csv_file)
    logger.info(f"Found {len(test_cases)} test cases in CSV")

    return _extract_cases_parallel(
        test_cases, projects_root, output_dir, jar_path, workers, desc="All extraction", jar_format=jar_format
    )


def main():
    """主函数 - 命令行工具"""
    parser = argparse.ArgumentParser(description='测试用例代码片段提取工具（多进程版）')
    parser.add_argument('--csv', required=True, help='CSV文件路径')
    parser.add_argument('--root', required=True, help='所有项目的根目录')
    parser.add_argument('--output', required=True, help='输出目录')
    parser.add_argument('--mode', choices=['top', 'specific', 'all'], default='top',
                        help='模式: top=提取前N个, specific=提取指定用例, all=提取全部')
    parser.add_argument('--top-n', type=int, default=100,
                        help='提取前N个用例（mode=top时有效，默认100）')
    parser.add_argument('--sort-by', choices=['oracle_length', 'assertion_count', 'mock_verify_count'],
                        default='oracle_length',
                        help='排序字段（mode=top时有效，默认oracle_length）')
    parser.add_argument('--test-names', nargs='+',
                        help='测试用例全名列表（mode=specific时有效）')
    parser.add_argument('--jar', default=None,
                        help='maven-test-metrics jar 路径（启用jar模式提取代码）')
    parser.add_argument('--format', choices=['annotated', 'raw-java'], default='annotated',
                        help='jar 模式输出格式：annotated=带标注的txt（默认）, raw-java=纯代码java（用于LLM benchmark）')
    parser.add_argument('--workers', type=int, default=4,
                        help='并行进程数（默认4）')

    args = parser.parse_args()

    csv_file = Path(args.csv)
    projects_root = Path(args.root)
    output_dir = Path(args.output)
    jar_path = Path(args.jar) if args.jar else None
    jar_format = args.format if jar_path else "annotated"

    if not csv_file.exists():
        logger.error(f"CSV file not found: {csv_file}")
        return 1

    if not projects_root.exists():
        logger.error(f"Projects root not found: {projects_root}")
        return 1

    if jar_path and not jar_path.exists():
        logger.error(f"Jar file not found: {jar_path}")
        return 1

    if args.mode == 'top':
        extract_top_n_test_cases(
            csv_file,
            projects_root,
            output_dir,
            top_n=args.top_n,
            sort_by=args.sort_by,
            jar_path=jar_path,
            workers=args.workers,
            jar_format=jar_format,
        )
    elif args.mode == 'specific':
        if not args.test_names:
            logger.error("--test-names is required when mode=specific")
            return 1

        extract_specific_test_cases(
            csv_file,
            projects_root,
            output_dir,
            test_names=args.test_names,
            jar_path=jar_path,
            workers=args.workers,
            jar_format=jar_format,
        )
    elif args.mode == 'all':
        extract_all_test_cases(
            csv_file,
            projects_root,
            output_dir,
            jar_path=jar_path,
            workers=args.workers,
            jar_format=jar_format,
        )

    return 0


if __name__ == '__main__':
    exit(main())
