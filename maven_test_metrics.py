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
from typing import List, Set, Dict, Optional, Any

import javalang
from tqdm import tqdm

from utils.logger_manager import get_logger


@dataclass
class TestMetrics:
    """测试用例指标数据类"""
    project_name: str
    test_full_name: str  # 格式: com.example.MyTest#testMethod
    setup_length: int  # setup_length：展开后全部测试方法的非断言有效代码行之和
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

    def __init__(self, project_root: Path, project_packages: Set[str],
                 source_class_set: Set[str] = None):
        self.project_root = project_root
        self.project_packages = project_packages
        # 生产类 FQN 精确集合（只含 src/main/java 中实际存在的类）
        # 若提供则用精确匹配，否则退回包名前缀匹配
        self.source_class_set: Set[str] = source_class_set or set()
        self.logger = get_logger('maven_test_metrics.log', 'TestMetrics')
        self._assertion_cache: Dict[str, bool] = {}  # 缓存：方法名 -> 是否是断言方法

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

    def is_assertion_method(self, method_name: str, all_methods: Dict,
                             visited: Set[str] = None) -> bool:
        """
        判断一个方法是否是断言方法。
        
        判断策略（按优先级）：
        1. 方法名以 'assert' 开头 → True
        2. 方法名等于 'fail' → True  
        3. 在 JUNIT_ASSERTIONS 集合中 → True
        4. 对于可展开的项目方法，递归检查其调用链（仅限方法名包含 assert/verify/expect/check）
        """
        if method_name in self._assertion_cache:
            return self._assertion_cache[method_name]

        if visited is None:
            visited = set()
        if method_name in visited:
            return False
        visited.add(method_name)

        # 策略1: 方法名语义判断（最高优先级）
        if method_name.startswith('assert'):
            self._assertion_cache[method_name] = True
            return True
        
        # 策略2: fail() 方法
        if method_name == 'fail':
            self._assertion_cache[method_name] = True
            return True
        
        # 策略3: JUnit 标准断言
        if method_name in self.JUNIT_ASSERTIONS:
            self._assertion_cache[method_name] = True
            return True

        # 策略4: 对于项目内可展开的方法，递归检查调用链
        # 注意：只有方法名看起来像断言的才递归，避免误判 addCard 等
        if method_name not in all_methods:
            # 外部方法（不在 all_methods 中），如果方法名不以 assert 开头，返回 False
            self._assertion_cache[method_name] = False
            return False
        
        # 对于项目方法，只递归检查方法名包含 assert/verify/expect 的方法
        if not any(keyword in method_name.lower() for keyword in ['assert', 'verify', 'expect', 'check']):
            # 方法名不像断言，不递归
            self._assertion_cache[method_name] = False
            return False
        
        # 展开调用链，递归检查
        method_node = all_methods[method_name]
        result = False
        for _, node in method_node.filter(javalang.tree.MethodInvocation):
            if self.is_assertion_method(node.member, all_methods, visited):
                result = True
                break

        self._assertion_cache[method_name] = result
        return result

    def find_first_assertion_line(self, method_node,
                                   all_methods: Dict) -> Optional[int]:
        """查找方法中第一个（真实）断言的行号"""
        min_line = None
        for _, node in method_node.filter(javalang.tree.MethodInvocation):
            if self.is_assertion_method(node.member, all_methods):
                if node.position and node.position.line:
                    if min_line is None or node.position.line < min_line:
                        min_line = node.position.line
        return min_line

    def count_assertions(self, expanded_nodes: List,
                          all_methods: Dict) -> int:
        """统计断言数量（在展开后的节点列表中）"""
        return sum(
            1 for _, node in expanded_nodes
            if isinstance(node, javalang.tree.MethodInvocation)
            and self.is_assertion_method(node.member, all_methods)
        )

    def expand_method_calls(self, method_node, all_methods: Dict,
                             expanded: Set[str] = None,
                             depth: int = 0, max_depth: int = 5,
                             expanded_names: Set[str] = None) -> List:
        """
        递归展开所有可解析的方法调用（含继承方法）。
        无 qualifier 且在 all_methods 中可找到的调用才展开（即测试类继承链上的方法）。
        有深度限制防止无限递归。

        参数:
            expanded_names: 可选，若传入则记录本次实际展开过的所有方法名（用于行数统计）
        """
        if expanded is None:
            expanded = set()
        if depth >= max_depth:
            return []

        all_nodes = []
        for path, node in method_node:
            all_nodes.append((path, node))
            if isinstance(node, javalang.tree.MethodInvocation):
                # 只展开无 qualifier 的调用（即继承方法或当前类方法）
                if (not node.qualifier
                        and node.member in all_methods
                        and node.member not in expanded):
                    expanded.add(node.member)
                    if expanded_names is not None:
                        expanded_names.add(node.member)
                    child_nodes = self.expand_method_calls(
                        all_methods[node.member], all_methods,
                        expanded, depth + 1, max_depth, expanded_names
                    )
                    all_nodes.extend(child_nodes)

        return all_nodes

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

    def _count_method_non_assertion_lines(self, method_node, source_lines: List[str],
                                           all_methods: Dict) -> int:
        """统计单个方法中非断言的有效代码行数（排除空行、注释、纯大括号行、断言行）"""
        if not method_node.position:
            return 0

        method_start = method_node.position.line
        method_end = method_start
        for _, node in method_node:
            if hasattr(node, 'position') and node.position:
                method_end = max(method_end, node.position.line)

        # 找出方法内所有断言行
        assertion_lines = set()
        for _, node in method_node.filter(javalang.tree.MethodInvocation):
            if self.is_assertion_method(node.member, all_methods) and node.position:
                assertion_lines.add(node.position.line)

        count = 0
        for line_num in range(method_start + 1, method_end + 1):
            if line_num <= len(source_lines) and line_num not in assertion_lines:
                line = source_lines[line_num - 1].strip()
                if line and not line.startswith('//') and not line.startswith('*') \
                        and not line.startswith('/*') and line not in ['{', '}', '{ }']:
                    count += 1
        return count

    def count_expanded_effective_lines(self, test_method_node, test_file: Path,
                                        expanded_names: Set[str],
                                        all_methods: Dict,
                                        all_methods_files: Dict[str, Path]) -> int:
        """
        统计展开后全部测试方法的有效非断言代码行总数。

        包含：
        1. 顶层测试方法本身的非断言有效行
        2. 所有被展开的测试继承链方法的非断言有效行
        """
        total = 0

        # 1. 顶层测试方法
        source_lines = self.get_source_code_lines(test_file)
        total += self._count_method_non_assertion_lines(test_method_node, source_lines, all_methods)

        # 2. 被展开的方法
        seen_files: Dict[Path, List[str]] = {test_file: source_lines}
        for method_name in expanded_names:
            if method_name not in all_methods or method_name not in all_methods_files:
                continue
            method_node = all_methods[method_name]
            file_path = all_methods_files[method_name]
            if file_path not in seen_files:
                seen_files[file_path] = self.get_source_code_lines(file_path)
            src_lines = seen_files[file_path]
            total += self._count_method_non_assertion_lines(method_node, src_lines, all_methods)

        return total

    def build_local_var_type_map(self, method_node) -> Dict[str, str]:
        """提取方法体中所有局部变量的声明类型: 变量名 -> 简单类型名"""
        var_type_map = {}
        for _, node in method_node.filter(javalang.tree.LocalVariableDeclaration):
            type_name = node.type.name
            for declarator in node.declarators:
                var_type_map[declarator.name] = type_name
        return var_type_map

    def build_combined_import_map(self, all_methods_files: Dict[str, Path]) -> Dict[str, str]:
        """构建继承链所有文件的合并 import_map（简单类名 -> FQN）。
        用于解析在父类文件中导入但子类文件中未导入的类型。
        """
        combined: Dict[str, str] = {}
        seen_files: Set[Path] = set()
        for file_path in all_methods_files.values():
            if file_path in seen_files:
                continue
            seen_files.add(file_path)
            tree = self.parse_java_file(file_path)
            if tree and tree.imports:
                for imp in tree.imports:
                    if not imp.static and not imp.wildcard:
                        simple_name = imp.path.split('.')[-1]
                        combined.setdefault(simple_name, imp.path)
        return combined

    def build_field_type_map(self, class_node, import_map: Dict[str, str],
                              package_name: str) -> Dict[str, str]:
        """提取类字段类型映射: 字段名 -> FQN 或 package.TypeName"""
        field_map = {}
        if class_node.fields:
            for field in class_node.fields:
                type_name = field.type.name
                # 优先从 import_map 解析 FQN，否则假设同包
                fqn = import_map.get(type_name)
                if not fqn:
                    fqn = f"{package_name}.{type_name}" if package_name else type_name
                for declarator in field.declarators:
                    field_map[declarator.name] = fqn
        return field_map

    def _collect_called_project_methods(self, expanded_nodes: List,
                                         tree, class_node,
                                         package_name: str,
                                         all_methods: Dict = None,
                                         method_node=None,
                                         inheritance_field_map: Dict[str, str] = None,
                                         all_methods_files: Dict[str, Path] = None) -> List[str]:
        """
        从展开后的节点列表中收集【生产代码】的方法调用。

        规则：
        - 无 qualifier 的调用（测试类继承链上的方法）→ 全部跳过，它们是测试基础设施
        - 有 qualifier 的调用 → 解析 qualifier 的类型，若属于生产代码包则收集

        参数:
            all_methods: 测试继承链全量方法表（用于判断 qualifier 是否是测试方法，以便跳过）
            method_node: 测试方法 AST 节点，用于提取局部变量类型映射
            inheritance_field_map: 继承链上所有字段的类型映射 field_name -> FQN
            all_methods_files: 继承链方法文件路径表，用于构建合并 import_map
        """
        # 构建当前文件的 import_map（简单类名 -> FQN）
        import_map = {}
        if tree.imports:
            for imp in tree.imports:
                if not imp.static and not imp.wildcard:
                    import_map[imp.path.split('.')[-1]] = imp.path

        # 合并继承链所有文件的 import_map（解析父类中导入但子类未导入的类型）
        combined_import_map = self.build_combined_import_map(all_methods_files) \
            if all_methods_files else import_map

        # 局部变量 & 方法形参类型映射：从展开后全部节点收集
        local_var_type_map: Dict[str, str] = {}
        for _, node in expanded_nodes:
            if isinstance(node, javalang.tree.LocalVariableDeclaration):
                for declarator in node.declarators:
                    local_var_type_map[declarator.name] = node.type.name
            elif isinstance(node, javalang.tree.FormalParameter):
                local_var_type_map[node.name] = node.type.name

        # 字段类型映射：优先使用继承链全量字段，退回到当前类字段
        if inheritance_field_map is not None:
            field_type_map = inheritance_field_map
        else:
            field_type_map = self.build_field_type_map(class_node, import_map, package_name)

        called_methods = []

        for _, node in expanded_nodes:
            if not isinstance(node, javalang.tree.MethodInvocation):
                continue
            qualifier, member = node.qualifier, node.member

            # 跳过 JUnit 断言、Mockito 验证和 Mock 方法（这些是测试框架方法）
            if member in self.JUNIT_ASSERTIONS or member in self.MOCKITO_VERIFY_METHODS \
                    or member in self.MOCKITO_MOCK_METHODS:
                continue

            # 无 qualifier：这些全是测试类继承链上的方法（addCard/execute 等），不是被测代码，跳过
            if not qualifier:
                continue

            # 有 qualifier：解析 qualifier 对应的完整类名
            full_class = None

            if qualifier in import_map:
                # qualifier 是类名，且在当前文件 import 中找到
                full_class = import_map[qualifier]
            elif qualifier in combined_import_map:
                # qualifier 在继承链某个父类文件中导入
                full_class = combined_import_map[qualifier]
            elif qualifier == class_node.name:
                # 当前类的静态方法
                full_class = f"{package_name}.{class_node.name}" if package_name else class_node.name
            elif qualifier in local_var_type_map:
                # qualifier 是局部变量
                type_name = local_var_type_map[qualifier]
                full_class = combined_import_map.get(type_name) or import_map.get(type_name)
                if not full_class:
                    full_class = f"{package_name}.{type_name}" if package_name else type_name
            elif qualifier in field_type_map:
                # qualifier 是字段（包括继承链上父类的字段）
                full_class = field_type_map[qualifier]
            elif '.' in qualifier:
                # 链式访问（如 CardRepository.instance.findCard），尝试解析根部分
                root = qualifier.split('.')[0]
                root_class = (combined_import_map.get(root) or import_map.get(root)
                               or field_type_map.get(root) or local_var_type_map.get(root))
                if root_class:
                    # 方法归属于根类型（近似：实际返回类型可能不同）
                    full_class = root_class
            # else: 无法解析，跳过

            if full_class and self._is_production_class(full_class):
                full_method = f"{full_class}.{member}"
                if full_method not in called_methods:
                    called_methods.append(full_method)

        return called_methods

    def _is_project_class(self, full_class_name: str) -> bool:
        """判断是否为项目内的类（包名前缀匹配，用于旧逻辑兼容）"""
        for package in self.project_packages:
            if full_class_name.startswith(package):
                return True
        return False

    def _is_production_class(self, full_class_name: str) -> bool:
        """
        判断是否为【生产代码】的类。
        若已建立 source_class_set，使用精确 FQN 匹配（最准确）；
        否则退回包名前缀匹配。
        """
        if self.source_class_set:
            return full_class_name in self.source_class_set
        return self._is_project_class(full_class_name)

    def analyze_test_method(self, test_file: Path, class_node,
                             method_node, tree, project_name: str,
                             all_methods: Dict,
                             all_methods_files: Dict[str, Path] = None,
                             inheritance_field_map: Dict[str, str] = None
                             ) -> Optional[TestMetrics]:
        """
        分析单个测试方法。

        参数:
            all_methods: 含继承链的全量测试方法表
            all_methods_files: 每个方法对应的源文件路径（用于展开后行数统计）
            inheritance_field_map: 继承链全量字段类型映射（用于解析 qualifier 类型）
        """
        try:
            # 每次分析新方法前清空断言缓存
            self._assertion_cache.clear()

            package_name = tree.package.name if tree.package else ""
            full_class_name = f"{package_name}.{class_node.name}" if package_name else class_node.name
            test_full_name = f"{full_class_name}#{method_node.name}"

            # 展开调用链（仅展开测试继承链上的方法），同时追踪展开的方法名
            expanded_names: Set[str] = set()
            expanded_nodes = self.expand_method_calls(
                method_node, all_methods, expanded_names=expanded_names
            )

            # setup_length：展开后全部测试方法的非断言有效代码行之和
            if all_methods_files is not None:
                setup_length = self.count_expanded_effective_lines(
                    method_node, test_file, expanded_names, all_methods, all_methods_files
                )
            else:
                # 退回到只统计顶层方法（兼容旧调用方式）
                source_lines = self.get_source_code_lines(test_file)
                setup_length = self._count_method_non_assertion_lines(
                    method_node, source_lines, all_methods
                )

            # 断言数量（在展开后的节点里统计）
            assertion_count = self.count_assertions(expanded_nodes, all_methods)

            # Mock 验证次数
            mock_verify_count = sum(
                1 for _, node in expanded_nodes
                if isinstance(node, javalang.tree.MethodInvocation)
                and node.member in self.MOCKITO_VERIFY_METHODS
            )

            # 是否使用 Mock
            uses_mock = self.check_uses_mock(tree, class_node)

            # 只收集【生产代码】的方法调用
            called_methods = self._collect_called_project_methods(
                expanded_nodes, tree, class_node, package_name, all_methods,
                method_node=method_node,
                inheritance_field_map=inheritance_field_map,
                all_methods_files=all_methods_files
            )

            return TestMetrics(
                project_name=project_name,
                test_full_name=test_full_name,
                setup_length=setup_length,
                assertion_count=assertion_count,
                mock_verify_count=mock_verify_count,
                uses_mock=uses_mock,
                called_project_methods=json.dumps(called_methods, ensure_ascii=False)
            )

        except Exception as e:
            self.logger.warning(f"Failed to analyze {method_node.name}: {e}")
            return None


class MavenProjectAnalyzer:
    """Maven项目分析器"""

    def __init__(self, project_root: Path, project_name: str):
        self.project_root = project_root
        self.project_name = project_name
        self.test_dirs = []
        self.source_dirs = []
        self.project_packages = set()   # 仅 src/main/java 的包（生产代码包）
        self.test_packages = set()      # 仅 src/test/java 的包（测试代码包）
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

                    # 添加所有父包（要求至少2个组件，避免 org/com/mage 这类单词误匹配）
                    parts = package.split('.')
                    for i in range(2, len(parts)):
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
                # 提取测试包名（测试代码的包）
                test_packages = self.extract_packages_from_source(test_dir)
                self.test_packages.update(test_packages)

            # 获取源码目录
            source_dir = self.get_source_directory(module)
            if source_dir:
                self.source_dirs.append(source_dir)
                self.logger.debug(f"Found source directory: {source_dir}")

                # 提取生产代码包名
                packages = self.extract_packages_from_source(source_dir)
                self.project_packages.update(packages)

        self.logger.info(f"Found {len(self.project_packages)} production packages, "
                         f"{len(self.test_packages)} test packages")
        self.logger.debug(f"Project packages: {sorted(self.project_packages)[:10]}...")

    def find_test_files(self) -> List[Path]:
        """查找所有测试文件"""
        test_files = []

        for test_dir in self.test_dirs:
            for java_file in test_dir.rglob('*.java'):
                # 简单过滤：文件名包含Test
                if 'Test' in java_file.name:
                    test_files.append(java_file)

        return test_files

    def build_class_index(self) -> Dict[str, Path]:
        """
        构建类型名索引：全类名 -> Path（仅顶层 class / interface / enum）。
        内部类/内部枚举跳过——它们的简单名可能与其他文件的同名内部类冲突。
        """
        class_index = {}
        analyzer = JavaCodeAnalyzer(self.project_root, self.project_packages)

        for source_dir in self.test_dirs + self.source_dirs:
            for java_file in source_dir.rglob('*.java'):
                tree = analyzer.parse_java_file(java_file)
                if not tree:
                    continue

                package_name = tree.package.name if tree.package else ""
                # tree.types 是编译单元的顶层类型声明列表，用 id 做精确过滤
                top_level_ids = {id(t) for t in (tree.types or [])}

                type_nodes = (
                    list(tree.filter(javalang.tree.ClassDeclaration))
                    + list(tree.filter(javalang.tree.InterfaceDeclaration))
                    + list(tree.filter(javalang.tree.EnumDeclaration))
                )
                for _, type_node in type_nodes:
                    if id(type_node) not in top_level_ids:
                        continue  # 跳过内部类/内部枚举
                    full_name = f"{package_name}.{type_node.name}" if package_name else type_node.name
                    if full_name in class_index and class_index[full_name] != java_file:
                        self.logger.warning(
                            f"Duplicate FQN detected: {full_name}\n"
                            f"  existing: {class_index[full_name]}\n"
                            f"  new:      {java_file}"
                        )
                    class_index[full_name] = java_file

        return class_index

    def get_all_methods_with_inheritance(self, class_node, tree,
                                          class_index: Dict[str, Path],
                                          visited: Set[str] = None,
                                          files_dict: Dict[str, Path] = None,
                                          fields_dict: Dict[str, str] = None) -> Dict[str, Any]:
        """
        递归收集当前类及所有父类的方法。

        参数:
            files_dict: 可选，若传入则同步填充 method_name -> 文件路径
            fields_dict: 可选，若传入则同步填充整个继承链的字段 field_name -> FQN类型
        """
        if visited is None:
            visited = set()

        all_methods = {}

        # 解析父类
        if class_node.extends:
            parent_class_name = class_node.extends.name

            # 通过 import 解析全类名
            full_parent_name = None
            if tree.imports:
                for imp in tree.imports:
                    if not imp.static and not imp.wildcard:
                        if imp.path.split('.')[-1] == parent_class_name:
                            full_parent_name = imp.path
                            break

            # 如果没找到，假设在同包下
            if not full_parent_name:
                package_name = tree.package.name if tree.package else ""
                full_parent_name = f"{package_name}.{parent_class_name}" if package_name else parent_class_name

            # 查找父类文件并递归
            if full_parent_name in class_index and full_parent_name not in visited:
                visited.add(full_parent_name)
                parent_file = class_index[full_parent_name]
                analyzer = JavaCodeAnalyzer(self.project_root, self.project_packages)
                parent_tree = analyzer.parse_java_file(parent_file)

                if parent_tree:
                    for _, parent_class_node in parent_tree.filter(javalang.tree.ClassDeclaration):
                        # 先递归收集父类的方法
                        parent_methods = self.get_all_methods_with_inheritance(
                            parent_class_node, parent_tree, class_index, visited,
                            files_dict, fields_dict
                        )
                        all_methods.update(parent_methods)

                        # 收集父类直接定义的方法
                        if parent_class_node.methods:
                            for method in parent_class_node.methods:
                                if method.name not in all_methods:  # 子类优先
                                    all_methods[method.name] = method
                                    if files_dict is not None:
                                        files_dict[method.name] = parent_file

                        # 收集父类字段（子类字段优先，此处用 setdefault 不覆盖）
                        if fields_dict is not None and parent_class_node.fields:
                            parent_import_map = {}
                            if parent_tree.imports:
                                for imp in parent_tree.imports:
                                    if not imp.static and not imp.wildcard:
                                        parent_import_map[imp.path.split('.')[-1]] = imp.path
                            parent_pkg = parent_tree.package.name if parent_tree.package else ""
                            for field in parent_class_node.fields:
                                type_name = field.type.name
                                fqn = parent_import_map.get(type_name)
                                if not fqn:
                                    fqn = f"{parent_pkg}.{type_name}" if parent_pkg else type_name
                                for declarator in field.declarators:
                                    fields_dict.setdefault(declarator.name, fqn)

        # 收集当前类的方法（覆盖父类同名方法）
        if class_node.methods:
            current_file = class_index.get(
                f"{(tree.package.name + '.') if tree.package else ''}{class_node.name}", None
            )
            for method in class_node.methods:
                all_methods[method.name] = method  # 子类方法优先
                if files_dict is not None and current_file:
                    files_dict[method.name] = current_file

        # 收集当前类字段（最高优先级，覆盖父类同名字段）
        if fields_dict is not None and class_node.fields:
            import_map = {}
            if tree.imports:
                for imp in tree.imports:
                    if not imp.static and not imp.wildcard:
                        import_map[imp.path.split('.')[-1]] = imp.path
            package_name = tree.package.name if tree.package else ""
            for field in class_node.fields:
                type_name = field.type.name
                fqn = import_map.get(type_name)
                if not fqn:
                    fqn = f"{package_name}.{type_name}" if package_name else type_name
                for declarator in field.declarators:
                    fields_dict[declarator.name] = fqn  # 当前类优先

        return all_methods

    def analyze_tests(self) -> List[TestMetrics]:
        """分析所有测试用例"""
        self.discover_project_structure()

        if not self.test_dirs:
            self.logger.warning(f"No test directories found for project: {self.project_name}")
            return []

        test_files = self.find_test_files()
        class_index = self.build_class_index()  # 构建类索引
        self.logger.info(f"Found {len(test_files)} test files, {len(class_index)} classes indexed")

        if not test_files:
            return []

        # 仅 src/main/java 中实际存在的类（精确 FQN 集合），用于区分生产代码与测试代码
        source_class_set: Set[str] = {
            fqn for fqn, path in class_index.items()
            if any(str(path).startswith(str(sd)) for sd in self.source_dirs)
        }
        self.logger.info(f"Source class set: {len(source_class_set)} classes from main/java")

        analyzer = JavaCodeAnalyzer(self.project_root, self.project_packages, source_class_set)
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

                    # 构建继承链全量方法表（同时填充 files_dict 和 fields_dict）
                    all_methods_files: Dict[str, Path] = {}
                    inheritance_field_map: Dict[str, str] = {}
                    all_methods = self.get_all_methods_with_inheritance(
                        class_node, tree, class_index,
                        files_dict=all_methods_files,
                        fields_dict=inheritance_field_map
                    )

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
                                test_file, class_node, method, tree, self.project_name,
                                all_methods,
                                all_methods_files=all_methods_files,
                                inheritance_field_map=inheritance_field_map
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
        'project_name', 'test_full_name', 'setup_length',
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