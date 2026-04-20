#!/usr/bin/env python3
"""
调试脚本：详细打印单个测试方法的分析过程，方便与真实代码对比验证。
用法：uv run python debug_analysis.py
"""
import json
import sys
from pathlib import Path
from typing import Dict, Set, List, Optional

import javalang

sys.path.insert(0, str(Path(__file__).parent))
from maven_test_metrics import JavaCodeAnalyzer, MavenProjectAnalyzer

# ── 配置 ──────────────────────────────────────────────────────────────────────
PROJECT_ROOT  = Path("/data/xuhaoran/pycharm/llm_test_github_collection/git/magefree/mage")
TARGET_FILE   = PROJECT_ROOT / "Mage.Tests/src/test/java/org/mage/test/cards/abilities/oneshot/destroy/UnlicensedDisintegrationTest.java"
TARGET_METHOD = "testDestroyCreatureLifeLoss"
PROJECT_NAME  = "magefree/mage"
# ─────────────────────────────────────────────────────────────────────────────


def sep(title=""):
    print("\n" + "=" * 70)
    if title:
        print(f"  {title}")
        print("=" * 70)


def _get_import_map(file_path: Path, analyzer: JavaCodeAnalyzer,
                    cache: Dict[Path, dict]) -> dict:
    if file_path not in cache:
        t = analyzer.parse_java_file(file_path)
        imap = {}
        if t and t.imports:
            for imp in t.imports:
                if not imp.static and not imp.wildcard:
                    imap[imp.path.split('.')[-1]] = imp.path
        cache[file_path] = imap
    return cache[file_path]


def _get_prod_calls(method_node, import_map: dict,
                    inheritance_field_map: dict,
                    analyzer: JavaCodeAnalyzer) -> Dict[str, int]:
    """收集该方法体内直接调用的生产代码方法，返回 call -> 调用次数。"""
    skip = (analyzer.JUNIT_ASSERTIONS
            | analyzer.MOCKITO_VERIFY_METHODS
            | analyzer.MOCKITO_MOCK_METHODS)
    counts: Dict[str, int] = {}
    for _, node in method_node:
        if not isinstance(node, javalang.tree.MethodInvocation) or not node.qualifier:
            continue
        q, m = node.qualifier, node.member
        if m in skip:
            continue
        full_class = import_map.get(q) or inheritance_field_map.get(q)
        if full_class and analyzer._is_production_class(full_class):
            key = f"{full_class}.{m}"
            counts[key] = counts.get(key, 0) + 1
    return counts


def _get_direct_test_children(method_node, all_methods: dict) -> List[str]:
    """收集该方法体内直接调用的测试继承链方法（保持首次出现顺序）。"""
    seen: Set[str] = set()
    children = []
    for _, node in method_node:
        if (isinstance(node, javalang.tree.MethodInvocation)
                and not node.qualifier
                and node.member in all_methods
                and node.member not in seen):
            children.append(node.member)
            seen.add(node.member)
    return children


def print_call_tree(method_name: str,
                    method_node,
                    file_path: Path,
                    all_methods: dict,
                    all_methods_files: Dict[str, Path],
                    analyzer: JavaCodeAnalyzer,
                    inheritance_field_map: dict,
                    file_cache: Dict[Path, List[str]],
                    import_map_cache: Dict[Path, dict],
                    visited: Set[str],
                    prefix: str = "",
                    is_last: bool = True,
                    is_root: bool = False):
    """递归打印方法调用树。"""

    # ── 行数统计 ──────────────────────────────────────
    if file_path not in file_cache:
        file_cache[file_path] = analyzer.get_source_code_lines(file_path)
    src_lines = file_cache[file_path]
    line_count = analyzer._count_method_non_assertion_lines(
        method_node, src_lines, all_methods)
    start_line = method_node.position.line if method_node.position else "?"

    # ── 打印当前节点 ──────────────────────────────────
    connector = "" if is_root else ("└── " if is_last else "├── ")
    print(f"{prefix}{connector}{method_name} [测试]  L{start_line}  {line_count}行")

    child_prefix = prefix if is_root else (
        prefix + ("    " if is_last else "│   ")
    )
    visited.add(method_name)

    # ── 生产代码调用 ──────────────────────────────────
    import_map = _get_import_map(file_path, analyzer, import_map_cache)
    prod_calls = _get_prod_calls(
        method_node, import_map, inheritance_field_map, analyzer)

    # ── 直接调用的测试子方法 ──────────────────────────
    test_children = _get_direct_test_children(method_node, all_methods)

    # 把生产调用和测试子方法合并按打印顺序排列（生产调用先）
    prod_items = [(k, v) for k, v in prod_calls.items()]
    child_items = test_children

    total = len(prod_items) + len(child_items)

    for idx, (call, count) in enumerate(prod_items):
        item_last = (idx == total - 1)
        ic = "└── " if item_last else "├── "
        cnt = f" (×{count})" if count > 1 else ""
        print(f"{child_prefix}{ic}[生产] {call}{cnt}")

    for idx, child_name in enumerate(child_items):
        global_idx = len(prod_items) + idx
        item_last = (global_idx == total - 1)
        ic = "└── " if item_last else "├── "
        next_prefix = child_prefix + ("    " if item_last else "│   ")

        child_node = all_methods.get(child_name)
        if child_node is None:
            print(f"{child_prefix}{ic}{child_name} [测试] (节点缺失)")
            continue

        child_file = all_methods_files.get(child_name)
        child_start = child_node.position.line if child_node.position else "?"

        if child_name in visited:
            # 已展开过，只显示引用
            print(f"{child_prefix}{ic}{child_name} [测试]  L{child_start}  (已展开↑)")
        elif child_file is None:
            print(f"{child_prefix}{ic}{child_name} [测试]  L{child_start}  (无文件路径)")
        else:
            print_call_tree(
                child_name, child_node, child_file,
                all_methods, all_methods_files,
                analyzer, inheritance_field_map,
                file_cache, import_map_cache,
                visited,
                prefix=child_prefix, is_last=item_last, is_root=False
            )


def main():
    # 1. 发现项目结构
    sep("1. 发现项目结构")
    proj_analyzer = MavenProjectAnalyzer(PROJECT_ROOT, PROJECT_NAME)
    for src_dir in PROJECT_ROOT.rglob("src/main/java"):
        if src_dir.is_dir():
            proj_analyzer.source_dirs.append(src_dir)
            proj_analyzer.project_packages.update(
                proj_analyzer.extract_packages_from_source(src_dir))
    for test_dir in PROJECT_ROOT.rglob("src/test/java"):
        if test_dir.is_dir():
            proj_analyzer.test_dirs.append(test_dir)
            proj_analyzer.test_packages.update(
                proj_analyzer.extract_packages_from_source(test_dir))
    print(f"生产包数量: {len(proj_analyzer.project_packages)}  "
          f"测试包数量: {len(proj_analyzer.test_packages)}")

    # 2. 构建类索引 & source_class_set
    sep("2. 构建类索引")
    class_index = proj_analyzer.build_class_index()
    source_class_set = {
        fqn for fqn, path in class_index.items()
        if any(str(path).startswith(str(sd)) for sd in proj_analyzer.source_dirs)
    }
    print(f"类索引总数: {len(class_index)}  生产类精确集合: {len(source_class_set)}")

    analyzer = JavaCodeAnalyzer(
        PROJECT_ROOT, proj_analyzer.project_packages, source_class_set)

    # 3. 解析目标文件
    sep("3. 解析目标文件 & 定位方法")
    tree = analyzer.parse_java_file(TARGET_FILE)
    if not tree:
        print("ERROR: 无法解析文件！")
        return

    target_class = target_method = None
    for _, cls in tree.filter(javalang.tree.ClassDeclaration):
        for m in (cls.methods or []):
            if m.name == TARGET_METHOD:
                target_class, target_method = cls, m
                break
        if target_method:
            break
    if not target_method:
        print(f"ERROR: 未找到 {TARGET_METHOD}")
        return
    print(f"类: {target_class.name}  方法: {target_method.name}  "
          f"L{target_method.position.line}")

    # 4. 构建继承链方法表 / 文件路径表 / 字段类型表
    sep("4. 构建继承链方法表 / 文件路径表 / 字段类型表")
    all_methods_files: Dict[str, Path] = {}
    inheritance_field_map: Dict[str, str] = {}
    all_methods = proj_analyzer.get_all_methods_with_inheritance(
        target_class, tree, class_index,
        files_dict=all_methods_files,
        fields_dict=inheritance_field_map,
    )
    print(f"全量方法数: {len(all_methods)}  有路径: {len(all_methods_files)}  "
          f"继承字段数: {len(inheritance_field_map)}")

    # ── 清断言缓存，供后续行数统计复用 ──────────────────
    analyzer._assertion_cache.clear()

    # 5. 方法调用树
    sep("5. 方法调用树")
    print("图例: [测试]=测试基础类方法(展开)  [生产]=被测生产代码方法(不展开)  (已展开↑)=该方法树已在上方\n")
    file_cache: Dict[Path, List[str]] = {}
    import_map_cache: Dict[Path, dict] = {}
    visited: Set[str] = set()
    print_call_tree(
        TARGET_METHOD, target_method, TARGET_FILE,
        all_methods, all_methods_files,
        analyzer, inheritance_field_map,
        file_cache, import_map_cache,
        visited,
        is_root=True,
    )

    # 6. 最终指标汇总
    sep("6. 最终测试指标")
    analyzer._assertion_cache.clear()

    expanded_names: Set[str] = set()
    expanded_nodes = analyzer.expand_method_calls(
        target_method, all_methods, expanded_names=expanded_names)

    setup_length = analyzer.count_expanded_effective_lines(
        target_method, TARGET_FILE, expanded_names, all_methods, all_methods_files)
    assertion_count = analyzer.count_assertions(expanded_nodes, all_methods)
    mock_verify_count = sum(
        1 for _, node in expanded_nodes
        if isinstance(node, javalang.tree.MethodInvocation)
        and node.member in analyzer.MOCKITO_VERIFY_METHODS
    )
    uses_mock = analyzer.check_uses_mock(tree, target_class)
    package_name = tree.package.name if tree.package else ""
    called_methods = analyzer._collect_called_project_methods(
        expanded_nodes, tree, target_class, package_name, all_methods,
        method_node=target_method,
        inheritance_field_map=inheritance_field_map,
    )

    print(f"setup_length          = {setup_length}")
    print(f"assertion_count       = {assertion_count}")
    print(f"mock_verify_count     = {mock_verify_count}")
    print(f"uses_mock             = {uses_mock}")
    print(f"called_project_methods= {json.dumps(called_methods, ensure_ascii=False, indent=2)}")


if __name__ == '__main__':
    main()
