#!/usr/bin/env python3
"""
测试用例代码片段提取工具

从CSV文件中读取测试用例信息，提取对应的源代码片段到单独的文件中。
"""

import csv
import os
import re
from pathlib import Path
from typing import Optional, List, Dict

from utils.logger_manager import get_logger


class ExtractTestSnippet:
    
    def __init__(self, log_path):
        self.logger = get_logger(log_path, 'ExtractTestSnippet')
     

    def sanitize_filename(self, name: str) -> str:
        """
        清理文件名，移除或替换非法字符
        
        Args:
            name: 原始文件名
        
        Returns:
            清理后的文件名
        """
        # 替换非法字符为下划线
        name = re.sub(r'[<>:"/\\|?*]', '_', name)
        # 替换 # 为双下划线
        name = name.replace('#', '__')
        # 替换空格
        name = name.replace(' ', '_')
        return name
    
    
    def extract_method_code(self, file_path: Path, class_name: str, method_name: str) -> Optional[str]:
        """
        从Java文件中提取指定方法的代码
        
        Args:
            file_path: Java文件路径
            class_name: 类名（简单类名，不含包名）
            method_name: 方法名
        
        Returns:
            方法的完整代码，如果未找到则返回None
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # 简单的方法提取逻辑（基于大括号匹配）
            # 查找方法声明模式，支持注解
            # 匹配模式: @Test ... public void methodName(...) {
            pattern = rf'(@\w+\s*(?:\([^)]*\))?\s*)*\s*(?:public|private|protected)?\s+(?:static\s+)?(?:\w+\s+)?{re.escape(method_name)}\s*\([^)]*\)\s*(?:throws\s+\w+(?:,\s*\w+)*)?\s*\{{'
            
            match = re.search(pattern, content)
            if not match:
                self.logger.warning(f"Method {method_name} not found in {file_path}")
                return None
            
            # 找到方法开始位置（从注解开始）
            method_start = match.start()
            
            # 找到方法体开始的大括号
            brace_start = match.end() - 1
            
            # 匹配大括号，找到方法结束位置
            brace_count = 1
            pos = brace_start + 1
            
            while pos < len(content) and brace_count > 0:
                if content[pos] == '{':
                    brace_count += 1
                elif content[pos] == '}':
                    brace_count -= 1
                pos += 1
            
            if brace_count == 0:
                method_code = content[method_start:pos]
                return method_code
            else:
                self.logger.warning(f"Unclosed braces for method {method_name} in {file_path}")
                return None
                
        except Exception as e:
            self.logger.error(f"Error extracting method {method_name} from {file_path}: {e}")
            return None
    
    
    def find_test_file(self, projects_root: Path, project_name: str, full_class_name: str) -> Optional[Path]:
        """
        根据项目名和完整类名查找测试文件
        
        Args:
            projects_root: 项目根目录
            project_name: 项目名（如 killme2008/aviatorscript）
            full_class_name: 完整类名（如 com.example.MyTest）
        
        Returns:
            测试文件路径，如果未找到则返回None
        """
        project_path = projects_root / project_name
        
        if not project_path.exists():
            self.logger.warning(f"Project path does not exist: {project_path}")
            return None
        
        # 将完整类名转换为文件路径
        # com.example.MyTest -> com/example/MyTest.java
        class_path = full_class_name.replace('.', '/') + '.java'
        
        # 递归查找所有可能的测试文件
        for java_file in project_path.rglob('*.java'):
            # 检查文件路径是否以class_path结尾
            if str(java_file).endswith(class_path.replace('/', os.sep)):
                # 确认是在测试目录中
                path_str = str(java_file)
                if '/test/' in path_str or '\\test\\' in path_str:
                    return java_file
        
        self.logger.warning(f"Test file not found for {full_class_name} in project {project_name}")
        return None
    
    
    def extract_test_case_code(
        self, 
        csv_row: Dict[str, str],
        projects_root: Path,
        output_dir: Path
    ) -> bool:
        """
        提取单个测试用例的代码并保存到文件
        
        Args:
            csv_row: CSV行数据字典
            projects_root: 所有项目的根目录
            output_dir: 输出目录
        
        Returns:
            是否成功提取
        """
        project_name = csv_row['project_name']
        test_full_name = csv_row['test_full_name']
        
        # 解析测试用例全名: com.example.MyTest#testMethod
        if '#' not in test_full_name:
            self.logger.warning(f"Invalid test name format: {test_full_name}")
            return False
        
        full_class_name, method_name = test_full_name.split('#', 1)
        class_name = full_class_name.split('.')[-1]  # 获取简单类名
        
        # 查找测试文件
        test_file = self.find_test_file(projects_root, project_name, full_class_name)
        if not test_file:
            return False
        
        # 提取方法代码
        method_code = self.extract_method_code(test_file, class_name, method_name)
        if not method_code:
            return False
        
        # 生成输出文件名
        # 格式: projectname__classname__methodname.java
        safe_project_name = self.sanitize_filename(project_name.replace('/', '_'))
        safe_class_name = self.sanitize_filename(class_name)
        safe_method_name = self.sanitize_filename(method_name)
        
        output_filename = f"{safe_project_name}__{safe_class_name}__{safe_method_name}.java"
        output_file = output_dir / output_filename
        
        # 保存代码片段
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            
            # 添加文件头注释
            header = f"""// Project: {project_name}
    // Test: {test_full_name}
    // Oracle Length: {csv_row.get('oracle_length', 'N/A')}
    // Assertions: {csv_row.get('assertion_count', 'N/A')}
    // Mock Verifications: {csv_row.get('mock_verify_count', 'N/A')}
    // Uses Mock: {csv_row.get('uses_mock', 'N/A')}
    // Called Methods: {csv_row.get('called_project_methods', 'N/A')}
    
    """
            
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(header)
                f.write(method_code)
            
            self.logger.info(f"Extracted: {output_filename}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error saving code to {output_file}: {e}")
            return False
    
    
    def extract_top_n_test_cases(
        self,
        csv_file: Path,
        projects_root: Path,
        output_dir: Path,
        top_n: int = 100,
        sort_by: str = 'oracle_length'
    ) -> int:
        """
        提取CSV中指标最大的前N个测试用例的代码
        
        Args:
            csv_file: CSV文件路径
            projects_root: 所有项目的根目录
            output_dir: 输出目录
            top_n: 提取的用例数量
            sort_by: 排序字段 ('oracle_length', 'assertion_count', 'mock_verify_count')
        
        Returns:
            成功提取的用例数量
        """
        self.logger.info(f"Reading CSV file: {csv_file}")
        
        # 读取CSV
        test_cases = []
        try:
            with open(csv_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    test_cases.append(row)
        except Exception as e:
            self.logger.error(f"Error reading CSV file: {e}")
            return 0
        
        self.logger.info(f"Found {len(test_cases)} test cases in CSV")
        
        # 按指定字段排序
        if sort_by not in ['oracle_length', 'assertion_count', 'mock_verify_count']:
            self.logger.warning(f"Invalid sort_by field: {sort_by}, using 'oracle_length'")
            sort_by = 'oracle_length'
        
        # 转换为整数并排序
        for tc in test_cases:
            try:
                tc[f'{sort_by}_int'] = int(tc.get(sort_by, 0))
            except (ValueError, TypeError):
                tc[f'{sort_by}_int'] = 0
        
        test_cases.sort(key=lambda x: x[f'{sort_by}_int'], reverse=True)
        
        # 提取前N个
        top_cases = test_cases[:top_n]
        self.logger.info(f"Extracting top {len(top_cases)} test cases by {sort_by}")
        
        success_count = 0
        for i, test_case in enumerate(top_cases, 1):
            self.logger.info(f"Processing {i}/{len(top_cases)}: {test_case['test_full_name']} ({sort_by}={test_case[sort_by]})")
            
            if self.extract_test_case_code(test_case, projects_root, output_dir):
                success_count += 1
        
        self.logger.info(f"Successfully extracted {success_count}/{len(top_cases)} test cases")
        return success_count
    
    
    def extract_specific_test_cases(
        self, 
        csv_file: Path,
        projects_root: Path,
        output_dir: Path,
        test_names: List[str]
    ) -> int:
        """
        提取指定的测试用例代码
        
        Args:
            csv_file: CSV文件路径
            projects_root: 所有项目的根目录
            output_dir: 输出目录
            test_names: 测试用例全名列表（格式: com.example.MyTest#testMethod）
        
        Returns:
            成功提取的用例数量
        """
        self.logger.info(f"Reading CSV file: {csv_file}")
        
        # 读取CSV并建立索引
        test_cases_map = {}
        try:
            with open(csv_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    test_cases_map[row['test_full_name']] = row
        except Exception as e:
            self.logger.error(f"Error reading CSV file: {e}")
            return 0
        
        self.logger.info(f"Found {len(test_cases_map)} test cases in CSV")
        
        success_count = 0
        for test_name in test_names:
            if test_name not in test_cases_map:
                self.logger.warning(f"Test case not found in CSV: {test_name}")
                continue
            
            self.logger.info(f"Processing: {test_name}")
            test_case = test_cases_map[test_name]
            
            if self.extract_test_case_code(test_case, projects_root, output_dir):
                success_count += 1
        
        self.logger.info(f"Successfully extracted {success_count}/{len(test_names)} test cases")
        return success_count


def main():
    """主函数 - 命令行工具"""
    import argparse
    
    parser = argparse.ArgumentParser(description='测试用例代码片段提取工具')
    parser.add_argument('--csv', required=True, help='CSV文件路径')
    parser.add_argument('--root', required=True, help='所有项目的根目录')
    parser.add_argument('--output', required=True, help='输出目录')
    parser.add_argument('--mode', choices=['top', 'specific'], default='top',
                       help='模式: top=提取前N个, specific=提取指定用例')
    parser.add_argument('--top-n', type=int, default=100,
                       help='提取前N个用例（mode=top时有效，默认100）')
    parser.add_argument('--sort-by', choices=['oracle_length', 'assertion_count', 'mock_verify_count'],
                       default='oracle_length',
                       help='排序字段（mode=top时有效，默认oracle_length）')
    parser.add_argument('--test-names', nargs='+',
                       help='测试用例全名列表（mode=specific时有效）')
    parser.add_argument('--log-file', help='可选的日志文件路径')

    args = parser.parse_args()

    # 在命令行运行时配置日志，但如果模块被导入（如测试），不会自动配置
    snippet = ExtractTestSnippet(args.log_file)

    csv_file = Path(args.csv)
    projects_root = Path(args.root)
    output_dir = Path(args.output)
    
    if not csv_file.exists():
        snippet.logger.error(f"CSV file not found: {csv_file}")
        return 1
    
    if not projects_root.exists():
        snippet.logger.error(f"Projects root not found: {projects_root}")
        return 1

    if args.mode == 'top':
        snippet.extract_top_n_test_cases(
            csv_file,
            projects_root,
            output_dir,
            top_n=args.top_n,
            sort_by=args.sort_by
        )
    elif args.mode == 'specific':
        if not args.test_names:
            snippet.logger.error("--test-names is required when mode=specific")
            return 1

        snippet.extract_specific_test_cases(
            csv_file,
            projects_root,
            output_dir,
            test_names=args.test_names
        )
    
    return 0


if __name__ == '__main__':
    exit(main())
