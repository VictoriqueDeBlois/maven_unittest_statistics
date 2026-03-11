#!/usr/bin/env python3
"""
Maven项目测试用例指标统计工具

统计指标：
1. 测试预言长度（断言前的代码量，排除空行和注释）
2. 断言数量
3. Mock验证次数
4. 是否使用Mock
5. 调用的项目内方法列表
"""

import csv
import json
import re
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Set, Dict, Optional

import javalang
from tqdm import tqdm

from utils.logger_manager import get_logger


@dataclass
class TestMetrics:
    """测试用例指标数据类"""
    project_name: str
    test_full_name: str  # 格式: com.example.MyTest#testMethod
    oracle_length: int  # 测试预言长度（断言前的代码行数）
    assertion_count: int  # 断言数量
    mock_verify_count: int  # Mock验证次数
    uses_mock: bool  # 是否使用Mock
    called_project_methods: str  # 调用的项目内方法列表（JSON格式）


class JavaCodeAnalyzer:
    """Java代码分析器"""

    # JUnit断言方法
    JUNIT_ASSERTIONS = {
        'assertEquals', 'assertNotEquals', 'assertTrue', 'assertFalse',
        'assertNull', 'assertNotNull', 'assertSame', 'assertNotSame',
        'assertArrayEquals', 'assertThrows', 'assertDoesNotThrow',
        'assertTimeout', 'assertTimeoutPreemptively', 'assertAll',
        'assertLinesMatch', 'assertIterableEquals', 'fail',
        'assertThat'  # Hamcrest/AssertJ
    }

    # Mockito Mock相关方法
    MOCKITO_MOCK_METHODS = {
        'mock', 'spy', 'when', 'doReturn', 'doThrow', 'doAnswer',
        'doNothing', 'doCallRealMethod', 'thenReturn', 'thenThrow',
        'thenAnswer', 'thenCallRealMethod'
    }

    # Mockito验证方法
    MOCKITO_VERIFY_METHODS = {'verify', 'verifyNoMoreInteractions', 'verifyNoInteractions'}

    def __init__(self, project_root: Path, project_packages: Set[str]):
        self.project_root = project_root
        self.project_packages = project_packages
        self.logger = get_logger('maven_test_metrics.log', 'TestMetrics')

    def parse_java_file(self, file_path: Path) -> Optional[javalang.tree.CompilationUnit]:
        """解析Java文件为AST"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            return javalang.parse.parse(content)
        except Exception as e:
            self.logger.warning(f"Failed to parse {file_path}: {e}")
            return None

    def get_source_code_lines(self, file_path: Path) -> List[str]:
        """获取源代码行（用于行号定位）"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.readlines()
        except Exception as e:
            self.logger.warning(f"Failed to read {file_path}: {e}")
            return []

    def is_empty_or_comment(self, line: str) -> bool:
        """判断是否为空行或注释行"""
        line = line.strip()
        if not line:
            return True
        if line.startswith('//'):
            return True
        if line.startswith('/*') or line.startswith('*'):
            return True
        return False

    def count_effective_lines(self, lines: List[str], start_line: int, end_line: int) -> int:
        """
        统计有效代码行数（排除空行和注释）
        注意：javalang的行号是1-based的
        """
        count = 0
        in_block_comment = False

        for i in range(start_line - 1, min(end_line, len(lines))):
            line = lines[i].strip()

            # 跳过空行
            if not line:
                continue

            # 处理块注释
            if '/*' in line:
                in_block_comment = True
            if in_block_comment:
                if '*/' in line:
                    in_block_comment = False
                continue

            # 跳过单行注释
            if line.startswith('//'):
                continue

            count += 1

        return count

    def find_first_assertion_line(self, method_node) -> Optional[int]:
        """查找方法中第一个断言语句的行号"""
        min_line = None

        # 使用filter递归遍历方法节点中的所有子节点
        for path, node in method_node.filter(javalang.tree.MethodInvocation):
            method_name = node.member
            if method_name in self.JUNIT_ASSERTIONS:
                if node.position and node.position.line:
                    if min_line is None or node.position.line < min_line:
                        min_line = node.position.line

        return min_line

    def count_assertions(self, method_node) -> int:
        """统计断言数量"""
        count = 0
        for path, node in method_node.filter(javalang.tree.MethodInvocation):
            if node.member in self.JUNIT_ASSERTIONS:
                count += 1
        return count

    def count_mock_verifications(self, method_node) -> int:
        """统计Mock验证次数"""
        count = 0
        for path, node in method_node.filter(javalang.tree.MethodInvocation):
            if node.member in self.MOCKITO_VERIFY_METHODS:
                count += 1
        return count

    def check_uses_mock(self, tree: javalang.tree.CompilationUnit, class_node) -> bool:
        """检查是否使用了Mock"""
        # 检查导入
        if tree.imports:
            for imp in tree.imports:
                if 'mockito' in imp.path.lower() or 'mock' in imp.path.lower():
                    return True

        # 检查注解
        if class_node.fields:
            for field in class_node.fields:
                if field.annotations:
                    for ann in field.annotations:
                        if 'Mock' in ann.name or 'InjectMocks' in ann.name:
                            return True

        # 检查方法调用 - 遍历整个类
        for path, node in class_node.filter(javalang.tree.MethodInvocation):
            if node.member in self.MOCKITO_MOCK_METHODS:
                return True

        return False

    def get_called_project_methods(self, method_node, tree: javalang.tree.CompilationUnit, class_node) -> List[str]:
        """获取调用的项目内方法列表"""
        called_methods = []

        # 构建导入映射（简化类名 -> 完整类名）
        import_map = {}
        static_imports = set()  # 静态导入的方法

        if tree.imports:
            for imp in tree.imports:
                if imp.static:
                    # 静态导入，只记录项目内的类
                    if imp.wildcard:
                        # import static Class.*
                        if self._is_project_class(imp.path):
                            static_imports.add(imp.path)
                    else:
                        # import static Class.method
                        parts = imp.path.split('.')
                        class_name = '.'.join(parts[:-1])
                        if self._is_project_class(class_name):
                            static_imports.add(class_name)
                else:
                    # 普通导入
                    parts = imp.path.split('.')
                    if not imp.wildcard:
                        simple_name = parts[-1]
                        import_map[simple_name] = imp.path

        # 获取当前包名和类名
        package_name = tree.package.name if tree.package else ""
        current_class_name = f"{package_name}.{class_node.name}" if package_name else class_node.name

        for path, node in method_node.filter(javalang.tree.MethodInvocation):
            qualifier = node.qualifier
            member = node.member

            full_method = None

            if qualifier:
                # 有限定符，如 obj.method() 或 ClassName.method()
                if qualifier in import_map:
                    # 是导入的类的静态方法调用
                    full_class = import_map[qualifier]
                    if self._is_project_class(full_class):
                        full_method = f"{full_class}.{member}"
                elif qualifier == class_node.name:
                    # 当前类的方法
                    if self._is_project_class(current_class_name):
                        full_method = f"{current_class_name}.{member}"
                else:
                    # 可能是对象调用或其他情况
                    # 尝试在同包下查找类
                    possible_class = f"{package_name}.{qualifier}" if package_name else qualifier
                    if self._is_project_class(possible_class):
                        full_method = f"{possible_class}.{member}"
            else:
                # 无限定符，可能是：
                # 1. 当前类的方法
                # 2. 静态导入的方法
                # 3. 同包下其他类的静态方法

                # 排除测试框架的方法（断言、Mock等）
                if member in self.JUNIT_ASSERTIONS or member in self.MOCKITO_VERIFY_METHODS or member in self.MOCKITO_MOCK_METHODS:
                    continue

                # 检查是否是静态导入的方法
                for static_class in static_imports:
                    if self._is_project_class(static_class):
                        full_method = f"{static_class}.{member}"
                        break

                # 如果不是静态导入，可能是当前类或父类的方法
                if not full_method and self._is_project_class(current_class_name):
                    # 检查是否是当前类的方法
                    is_current_class_method = False
                    if class_node.methods:
                        for m in class_node.methods:
                            if m.name == member:
                                is_current_class_method = True
                                break

                    if is_current_class_method:
                        full_method = f"{current_class_name}.{member}"

            if full_method and full_method not in called_methods:
                called_methods.append(full_method)

        return called_methods

    def _is_project_class(self, full_class_name: str) -> bool:
        """判断是否为项目内的类"""
        for package in self.project_packages:
            if full_class_name.startswith(package):
                return True
        return False

    def get_private_methods(self, class_node) -> Dict[str, any]:
        """获取类中的所有private方法"""
        private_methods = {}

        if class_node.methods:
            for method in class_node.methods:
                if method.modifiers and 'private' in method.modifiers:
                    private_methods[method.name] = method

        return private_methods

    def expand_private_method_calls(self, method_node, private_methods: Dict,
                                    expanded: Set[str] = None) -> List:
        """
        递归展开private方法调用
        返回展开后的所有节点
        """
        if expanded is None:
            expanded = set()

        all_nodes = []

        # 收集当前方法的所有节点
        for path, node in method_node:
            all_nodes.append((path, node))

            if isinstance(node, javalang.tree.MethodInvocation):
                method_name = node.member

                # 如果调用的是private方法且未展开过
                if method_name in private_methods and method_name not in expanded:
                    expanded.add(method_name)
                    private_method = private_methods[method_name]

                    # 递归展开
                    expanded_nodes = self.expand_private_method_calls(
                        private_method, private_methods, expanded
                    )
                    all_nodes.extend(expanded_nodes)

        return all_nodes

    def analyze_test_method(self, test_file: Path, class_node, method_node,
                            tree: javalang.tree.CompilationUnit, project_name: str) -> Optional[TestMetrics]:
        """分析单个测试方法"""
        try:
            # 获取完整类名
            package_name = tree.package.name if tree.package else ""
            class_name = class_node.name
            full_class_name = f"{package_name}.{class_name}" if package_name else class_name
            test_full_name = f"{full_class_name}#{method_node.name}"

            # 获取private方法
            private_methods = self.get_private_methods(class_node)

            # 展开private方法调用（获取所有节点，包括展开的private方法）
            expanded_nodes = self.expand_private_method_calls(method_node, private_methods)

            # 查找第一个断言的行号（在原始方法中，不在展开的private方法中）
            first_assertion_line = self.find_first_assertion_line(method_node)

            # 计算测试预言长度
            oracle_length = 0
            if first_assertion_line and method_node.position:
                source_lines = self.get_source_code_lines(test_file)
                start_line = method_node.position.line + 1  # 跳过方法签名
                oracle_length = self.count_effective_lines(source_lines, start_line, first_assertion_line - 1)

            # 统计断言数量（包括展开的private方法）
            assertion_count = sum(1 for _, node in expanded_nodes
                                  if isinstance(node, javalang.tree.MethodInvocation)
                                  and node.member in self.JUNIT_ASSERTIONS)

            # 统计Mock验证次数（包括展开的private方法）
            mock_verify_count = sum(1 for _, node in expanded_nodes
                                    if isinstance(node, javalang.tree.MethodInvocation)
                                    and node.member in self.MOCKITO_VERIFY_METHODS)

            # 检查是否使用Mock
            uses_mock = self.check_uses_mock(tree, class_node)

            # 获取调用的项目内方法（包括展开的private方法）
            called_methods = []

            # 构建导入映射
            import_map = {}
            static_imports = set()

            if tree.imports:
                for imp in tree.imports:
                    if imp.static:
                        # 静态导入
                        if imp.wildcard:
                            # import static Class.* 的情况
                            # 只记录项目内的类
                            if self._is_project_class(imp.path):
                                static_imports.add(imp.path)
                        else:
                            # import static Class.method 的情况
                            parts = imp.path.split('.')
                            class_name_import = '.'.join(parts[:-1])
                            if self._is_project_class(class_name_import):
                                static_imports.add(class_name_import)
                    else:
                        # 普通导入
                        parts = imp.path.split('.')
                        if not imp.wildcard:
                            simple_name = parts[-1]
                            import_map[simple_name] = imp.path

            current_class_full = f"{package_name}.{class_node.name}" if package_name else class_node.name

            for _, node in expanded_nodes:
                if isinstance(node, javalang.tree.MethodInvocation):
                    qualifier = node.qualifier
                    member = node.member

                    full_method = None

                    if qualifier:
                        # 有限定符的调用
                        if qualifier in import_map:
                            full_class = import_map[qualifier]
                            if self._is_project_class(full_class):
                                full_method = f"{full_class}.{member}"
                        elif qualifier == class_node.name:
                            if self._is_project_class(current_class_full):
                                full_method = f"{current_class_full}.{member}"
                        else:
                            # 尝试同包下的类
                            possible_class = f"{package_name}.{qualifier}" if package_name else qualifier
                            if self._is_project_class(possible_class):
                                full_method = f"{possible_class}.{member}"
                    else:
                        # 无限定符的调用
                        # 排除断言和Mock方法（这些是测试框架的方法，不是项目方法）
                        if member in self.JUNIT_ASSERTIONS or member in self.MOCKITO_VERIFY_METHODS or member in self.MOCKITO_MOCK_METHODS:
                            continue

                        # 检查静态导入
                        for static_class in static_imports:
                            if self._is_project_class(static_class):
                                full_method = f"{static_class}.{member}"
                                break

                        # 检查当前类的方法
                        if not full_method and self._is_project_class(current_class_full):
                            is_current_class_method = False
                            if class_node.methods:
                                for m in class_node.methods:
                                    if m.name == member:
                                        is_current_class_method = True
                                        break

                            if is_current_class_method:
                                full_method = f"{current_class_full}.{member}"

                    if full_method and full_method not in called_methods:
                        called_methods.append(full_method)

            return TestMetrics(
                project_name=project_name,
                test_full_name=test_full_name,
                oracle_length=oracle_length,
                assertion_count=assertion_count,
                mock_verify_count=mock_verify_count,
                uses_mock=uses_mock,
                called_project_methods=json.dumps(called_methods, ensure_ascii=False)
            )

        except Exception as e:
            self.logger.warning(f"Failed to analyze test method {method_node.name}: {e}")
            return None


class MavenProjectAnalyzer:
    """Maven项目分析器"""

    def __init__(self, project_root: Path, project_name: str):
        self.project_root = project_root
        self.project_name = project_name
        self.test_dirs = []
        self.source_dirs = []
        self.project_packages = set()
        self.logger = get_logger('maven_test_metrics.log', 'TestMetrics')

    def find_maven_modules(self) -> List[Path]:
        """查找所有Maven模块（包含pom.xml的目录）"""
        modules = []

        for pom_file in self.project_root.rglob('pom.xml'):
            module_dir = pom_file.parent
            modules.append(module_dir)

        return modules if modules else [self.project_root]

    def get_test_source_directory(self, module_dir: Path) -> Optional[Path]:
        """使用Maven命令获取测试源码目录"""
        try:
            # 使用Maven help插件获取测试源码目录
            result = subprocess.run(
                ['mvn', 'help:evaluate', '-Dexpression=project.build.testSourceDirectory',
                 '-q', '-DforceStdout'],
                cwd=module_dir,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                test_dir = result.stdout.strip()
                if test_dir and Path(test_dir).exists():
                    return Path(test_dir)

            # 如果Maven命令失败，使用默认路径
            default_test_dir = module_dir / 'src' / 'test' / 'java'
            if default_test_dir.exists():
                return default_test_dir

        except Exception as e:
            self.logger.warning(f"Failed to get test directory for {module_dir}: {e}")

            # 使用默认路径
            default_test_dir = module_dir / 'src' / 'test' / 'java'
            if default_test_dir.exists():
                return default_test_dir

        return None

    def get_source_directory(self, module_dir: Path) -> Optional[Path]:
        """获取源码目录"""
        try:
            result = subprocess.run(
                ['mvn', 'help:evaluate', '-Dexpression=project.build.sourceDirectory',
                 '-q', '-DforceStdout'],
                cwd=module_dir,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                source_dir = result.stdout.strip()
                if source_dir and Path(source_dir).exists():
                    return Path(source_dir)

            # 使用默认路径
            default_source_dir = module_dir / 'src' / 'main' / 'java'
            if default_source_dir.exists():
                return default_source_dir

        except Exception as e:
            self.logger.warning(f"Failed to get source directory for {module_dir}: {e}")

            default_source_dir = module_dir / 'src' / 'main' / 'java'
            if default_source_dir.exists():
                return default_source_dir

        return None

    def extract_packages_from_source(self, source_dir: Path) -> Set[str]:
        """从源码目录提取包名"""
        packages = set()

        for java_file in source_dir.rglob('*.java'):
            try:
                with open(java_file, 'r', encoding='utf-8') as f:
                    content = f.read()

                # 提取package声明
                match = re.search(r'package\s+([\w.]+)\s*;', content)
                if match:
                    package = match.group(1)
                    packages.add(package)

                    # 添加所有父包
                    parts = package.split('.')
                    for i in range(1, len(parts)):
                        parent_package = '.'.join(parts[:i])
                        packages.add(parent_package)

            except Exception as e:
                self.logger.debug(f"Failed to extract package from {java_file}: {e}")

        return packages

    def discover_project_structure(self):
        """发现项目结构"""
        self.logger.info(f"Discovering structure for project: {self.project_name}")

        modules = self.find_maven_modules()
        self.logger.info(f"Found {len(modules)} Maven modules")

        for module in modules:
            # 获取测试目录
            test_dir = self.get_test_source_directory(module)
            if test_dir:
                self.test_dirs.append(test_dir)
                self.logger.debug(f"Found test directory: {test_dir}")

            # 获取源码目录
            source_dir = self.get_source_directory(module)
            if source_dir:
                self.source_dirs.append(source_dir)
                self.logger.debug(f"Found source directory: {source_dir}")

                # 提取包名
                packages = self.extract_packages_from_source(source_dir)
                self.project_packages.update(packages)

        self.logger.info(f"Found {len(self.project_packages)} unique packages")
        self.logger.debug(f"Project packages: {sorted(self.project_packages)[:10]}...")  # 只显示前10个

    def find_test_files(self) -> List[Path]:
        """查找所有测试文件"""
        test_files = []

        for test_dir in self.test_dirs:
            for java_file in test_dir.rglob('*.java'):
                # 简单过滤：文件名包含Test
                if 'Test' in java_file.name:
                    test_files.append(java_file)

        return test_files

    def analyze_tests(self) -> List[TestMetrics]:
        """分析所有测试用例"""
        self.discover_project_structure()

        if not self.test_dirs:
            self.logger.warning(f"No test directories found for project: {self.project_name}")
            return []

        test_files = self.find_test_files()
        self.logger.info(f"Found {len(test_files)} test files")

        if not test_files:
            return []

        analyzer = JavaCodeAnalyzer(self.project_root, self.project_packages)
        all_metrics = []

        # 使用tqdm显示进度
        for test_file in tqdm(test_files, desc=f"Analyzing {self.project_name}", leave=False):
            try:
                tree = analyzer.parse_java_file(test_file)
                if not tree:
                    continue

                # 遍历所有类
                for _, class_node in tree.filter(javalang.tree.ClassDeclaration):
                    if not class_node.methods:
                        continue

                    # 遍历所有方法
                    for method in class_node.methods:
                        # 检查是否为测试方法（带@Test注解）
                        is_test = False
                        if method.annotations:
                            for ann in method.annotations:
                                if ann.name == 'Test':
                                    is_test = True
                                    break

                        if is_test:
                            metrics = analyzer.analyze_test_method(
                                test_file, class_node, method, tree, self.project_name
                            )
                            if metrics:
                                all_metrics.append(metrics)

            except Exception as e:
                self.logger.warning(f"Failed to analyze test file {test_file}: {e}")

        self.logger.info(f"Analyzed {len(all_metrics)} test cases for project: {self.project_name}")
        return all_metrics


def process_single_project(project_name: str, projects_root: Path) -> List[TestMetrics]:
    logger = get_logger('maven_test_metrics.log', 'TestMetrics')
    """处理单个项目（用于并行处理）"""
    try:
        project_path = projects_root / project_name

        if not project_path.exists():
            logger.error(f"Project path does not exist: {project_path}")
            return []

        logger.info(f"Processing project: {project_name}")
        analyzer = MavenProjectAnalyzer(project_path, project_name)
        return analyzer.analyze_tests()

    except Exception as e:
        logger.error(f"Error processing project {project_name}: {e}", exc_info=True)
        return []


def main():
    """主函数"""
    import argparse

    logger = get_logger('maven_test_metrics.log', 'TestMetrics') # 配置日志

    parser = argparse.ArgumentParser(description='Maven测试用例指标统计工具')
    parser.add_argument('--projects', required=True, help='项目名称列表文件（每行一个项目名，如 killme2008/aviatorscript）')
    parser.add_argument('--root', required=True, help='所有项目的根目录')
    parser.add_argument('--output', default='test_metrics.csv', help='输出CSV文件路径')
    parser.add_argument('--workers', type=int, default=4, help='并行处理的进程数')
    parser.add_argument('--resume', action='store_true', help='断点续传模式：跳过已分析的项目')
    parser.add_argument('--append', action='store_true', help='追加模式：在现有CSV文件后追加新结果')

    args = parser.parse_args()

    # 读取项目列表
    projects_file = Path(args.projects)
    if not projects_file.exists():
        logger.error(f"Projects file not found: {projects_file}")
        return

    with open(projects_file, 'r', encoding='utf-8') as f:
        project_names = [line.strip() for line in f if line.strip()]

    logger.info(f"Found {len(project_names)} projects to analyze")

    projects_root = Path(args.root)
    if not projects_root.exists():
        logger.error(f"Projects root directory not found: {projects_root}")
        return

    output_file = Path(args.output)

    # 断点续传：读取已处理的项目
    processed_projects = set()
    if args.resume and output_file.exists():
        logger.info("Resume mode: loading already processed projects...")
        try:
            with open(output_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    processed_projects.add(row['project_name'])
            logger.info(f"Found {len(processed_projects)} already processed projects")

            # 过滤掉已处理的项目
            original_count = len(project_names)
            project_names = [p for p in project_names if p not in processed_projects]
            logger.info(f"Skipping {original_count - len(project_names)} projects, {len(project_names)} remaining")

        except Exception as e:
            logger.warning(f"Failed to read existing results: {e}")
            processed_projects = set()

    if not project_names:
        logger.info("All projects already processed!")
        return

    # 准备CSV文件
    # 如果是追加模式且文件已存在，不写入header
    write_header = not (args.append and output_file.exists())

    # 打开CSV文件用于增量写入
    csv_file = open(output_file, 'a' if args.append else 'w', newline='', encoding='utf-8')
    csv_writer = csv.DictWriter(csv_file, fieldnames=[
        'project_name', 'test_full_name', 'oracle_length',
        'assertion_count', 'mock_verify_count', 'uses_mock',
        'called_project_methods'
    ])

    if write_header:
        csv_writer.writeheader()
        csv_file.flush()  # 立即刷新到磁盘

    total_test_cases = 0

    try:
        # 并行处理项目
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            # 提交所有任务
            future_to_project = {
                executor.submit(process_single_project, project_name, projects_root): project_name
                for project_name in project_names
            }

            # 使用tqdm显示总体进度
            with tqdm(total=len(project_names), desc="Processing projects") as pbar:
                for future in as_completed(future_to_project):
                    project_name = future_to_project[future]
                    try:
                        metrics = future.result()

                        # 立即写入CSV（一个项目处理完就写入）
                        for metric in metrics:
                            csv_writer.writerow(asdict(metric))

                        # 刷新到磁盘，确保数据不会丢失
                        csv_file.flush()

                        total_test_cases += len(metrics)
                        logger.info(f"Completed {project_name}: {len(metrics)} test cases (total: {total_test_cases})")

                    except Exception as e:
                        logger.error(f"Error in project {project_name}: {e}")
                    finally:
                        pbar.update(1)

        logger.info(f"Results written to {output_file}")
        logger.info(f"Total test cases analyzed: {total_test_cases}")

    finally:
        # 确保CSV文件被正确关闭
        csv_file.close()
        logger.info("CSV file closed")


if __name__ == '__main__':
    main()