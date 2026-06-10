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
                    analyzer: JavaCodeAnalyzer,
                    combined_import_map: dict = None) -> Dict[str, int]:
    """收集该方法体内直接调用的生产代码方法，返回 call -> 调用次数。"""
    skip = (analyzer.JUNIT_ASSERTIONS
            | analyzer.MOCKITO_VERIFY_METHODS
            | analyzer.MOCKITO_MOCK_METHODS)
    combined = combined_import_map or import_map

    # 收集方法体内局部变量类型 & 形参类型
    local_var: Dict[str, str] = {}
    for _, node in method_node.filter(javalang.tree.LocalVariableDeclaration):
        for declarator in node.declarators:
            local_var[declarator.name] = node.type.name
    for _, node in method_node.filter(javalang.tree.FormalParameter):
        local_var[node.name] = node.type.name

    counts: Dict[str, int] = {}
    for _, node in method_node:
        if not isinstance(node, javalang.tree.MethodInvocation) or not node.qualifier:
            continue
        q, m = node.qualifier, node.member
        if m in skip:
            continue
        full_class = import_map.get(q) or combined.get(q) or inheritance_field_map.get(q)
        if not full_class and q in local_var:
            type_name = local_var[q]
            full_class = combined.get(type_name) or import_map.get(type_name)
        if not full_class and '.' in q:
            root = q.split('.')[0]
            full_class = (import_map.get(root) or combined.get(root)
                          or inheritance_field_map.get(root) or local_var.get(root))
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
                    combined_import_map: dict = None,
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
        method_node, import_map, inheritance_field_map, analyzer, combined_import_map)

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
                combined_import_map=combined_import_map,
                prefix=child_prefix, is_last=item_last, is_root=False
            )


def _build_prod_calls_per_line(method_node,
                                import_map: dict,
                                combined_import_map: dict,
                                inheritance_field_map: dict,
                                analyzer: JavaCodeAnalyzer) -> Dict[int, List[str]]:
    """返回 {行号: [生产方法FQN, ...]}，用于在行注释中标注生产代码调用。"""
    skip = (analyzer.JUNIT_ASSERTIONS | analyzer.MOCKITO_VERIFY_METHODS
            | analyzer.MOCKITO_MOCK_METHODS)
    combined = combined_import_map or import_map
    field_map = inheritance_field_map or {}

    # 局部变量类型 & 形参类型（方法体内）
    local_var: Dict[str, str] = {}
    for _, node in method_node.filter(javalang.tree.LocalVariableDeclaration):
        for declarator in node.declarators:
            local_var[declarator.name] = node.type.name
    for _, node in method_node.filter(javalang.tree.FormalParameter):
        local_var[node.name] = node.type.name

    per_line: Dict[int, List[str]] = {}
    for _, node in method_node.filter(javalang.tree.MethodInvocation):
        if not node.qualifier or node.member in skip or not node.position:
            continue
        q = node.qualifier
        full_class = None
        if q in import_map:
            full_class = import_map[q]
        elif q in combined:
            full_class = combined[q]
        elif q in local_var:
            type_name = local_var[q]
            full_class = combined.get(type_name) or import_map.get(type_name)
        elif q in field_map:
            full_class = field_map[q]
        elif '.' in q:
            root = q.split('.')[0]
            full_class = (import_map.get(root) or combined.get(root)
                          or field_map.get(root) or local_var.get(root))
        if full_class and analyzer._is_production_class(full_class):
            ln = node.position.line
            call = f"{full_class}.{node.member}"
            per_line.setdefault(ln, [])
            if call not in per_line[ln]:
                per_line[ln].append(call)
    return per_line


def annotate_method_lines(method_node,
                           source_lines: List[str],
                           all_methods: dict,
                           analyzer: JavaCodeAnalyzer,
                           import_map: dict = None,
                           combined_import_map: dict = None,
                           inheritance_field_map: dict = None) -> tuple:
    """
    返回 (annotated_lines, counted_lines, assertion_invocations) 其中：
    - annotated_lines: List[str]，每行格式 "[TAG] line_num  raw_line  [← 生产调用]"
      TAG = 签名 | 计数 | 断言 | 跳过
    - counted_lines: int，实际计入 setup_length 的行数
    - assertion_invocations: int，断言方法调用次数（同一行多个断言各计一次）
    """
    if not method_node.position:
        return ["  (无位置信息)"], 0, 0

    method_start = method_node.position.line
    method_end = method_start
    for _, node in method_node:
        if hasattr(node, 'position') and node.position:
            method_end = max(method_end, node.position.line)

    # 断言行 & 断言调用次数
    assertion_lines: Set[int] = set()
    assertion_invocations = 0
    for _, node in method_node.filter(javalang.tree.MethodInvocation):
        if analyzer.is_assertion_method(node.member, all_methods) and node.position:
            assertion_lines.add(node.position.line)
            assertion_invocations += 1

    # 生产代码调用（按行）
    prod_per_line = _build_prod_calls_per_line(
        method_node,
        import_map or {},
        combined_import_map or import_map or {},
        inheritance_field_map or {},
        analyzer,
    )

    result = []
    counted = 0
    for ln in range(method_start, method_end + 1):
        if ln > len(source_lines):
            break
        raw = source_lines[ln - 1].rstrip('\n')
        s = raw.strip()

        if ln == method_start:
            tag = "签名"
        elif ln in assertion_lines:
            tag = "断言"
        elif (not s
              or s.startswith('//')
              or s.startswith('*')
              or s.startswith('/*')
              or s in ('{', '}', '{ }')):
            tag = "跳过"
        else:
            tag = "计数"
            counted += 1

        prod_suffix = ""
        if ln in prod_per_line:
            prod_suffix = "  ← [生产] " + ", ".join(prod_per_line[ln])
        result.append(f"[{tag}] {ln:5d}  {raw}{prod_suffix}")

    return result, counted, assertion_invocations


def dump_effective_lines(test_method_name: str,
                          test_method_node,
                          test_file: Path,
                          expanded_names: Set[str],
                          all_methods: dict,
                          all_methods_files: Dict[str, Path],
                          analyzer: JavaCodeAnalyzer,
                          output_dir: Path,
                          combined_import_map: dict = None,
                          inheritance_field_map: dict = None):
    """
    把顶层测试方法 + 所有展开子方法的有效代码逐行写入 debug/<method_name>.txt。
    每行标注 [签名] / [计数] / [断言] / [跳过]，生产代码调用行追加 ← [生产] FQN。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    out_file = output_dir / f"{test_method_name}.txt"

    file_src_cache: Dict[Path, List[str]] = {}
    import_map_cache: Dict[Path, dict] = {}

    def get_src(fp: Path) -> List[str]:
        if fp not in file_src_cache:
            file_src_cache[fp] = analyzer.get_source_code_lines(fp)
        return file_src_cache[fp]

    def get_import_map(fp: Path) -> dict:
        if fp not in import_map_cache:
            t = analyzer.parse_java_file(fp)
            imap = {}
            if t and t.imports:
                for imp in t.imports:
                    if not imp.static and not imp.wildcard:
                        imap[imp.path.split('.')[-1]] = imp.path
            import_map_cache[fp] = imap
        return import_map_cache[fp]

    # 顶层方法排首位，然后按展开顺序输出各子方法
    methods_to_dump = [(test_method_name, test_method_node, test_file)]
    for name in expanded_names:
        if name in all_methods and name in all_methods_files:
            methods_to_dump.append((name, all_methods[name], all_methods_files[name]))

    grand_setup = 0
    grand_assert = 0
    with open(out_file, 'w', encoding='utf-8') as f:
        f.write(f"# 测试方法: {test_method_name}\n")
        f.write(f"# 标注说明: [计数]=计入setup_length  [断言]=断言行(不计入)  "
                f"[跳过]=空行/注释/大括号(不计入)  [签名]=方法签名(不计入)\n"
                f"#           ← [生产] FQN  标注该行调用了哪个生产代码方法\n\n")

        for method_name, method_node, file_path in methods_to_dump:
            start_ln = method_node.position.line if method_node.position else '?'
            rel_path = file_path.relative_to(PROJECT_ROOT) if file_path else file_path
            f.write(f"{'=' * 70}\n")
            f.write(f"方法: {method_name}  ({rel_path}:{start_ln})\n")
            f.write(f"{'=' * 70}\n")

            src = get_src(file_path)
            imap = get_import_map(file_path)
            lines, counted, assert_cnt = annotate_method_lines(
                method_node, src, all_methods, analyzer,
                import_map=imap,
                combined_import_map=combined_import_map,
                inheritance_field_map=inheritance_field_map,
            )
            for line in lines:
                f.write(line + '\n')
            f.write(f"\n>>> 本方法计入行数: {counted}  断言调用次数: {assert_cnt}\n\n")
            grand_setup += counted
            grand_assert += assert_cnt

        f.write(f"{'=' * 70}\n")
        f.write(f">>> setup_length 合计: {grand_setup}  assertion_count 合计: {grand_assert}\n")

    print(f"\n[debug] 有效代码注释文件已写入: {out_file}")


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
    combined_import_map = analyzer.build_combined_import_map(all_methods_files)
    print_call_tree(
        TARGET_METHOD, target_method, TARGET_FILE,
        all_methods, all_methods_files,
        analyzer, inheritance_field_map,
        file_cache, import_map_cache,
        visited,
        combined_import_map=combined_import_map,
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
        all_methods_files=all_methods_files,
    )

    print(f"setup_length          = {setup_length}")
    print(f"assertion_count       = {assertion_count}")
    print(f"mock_verify_count     = {mock_verify_count}")
    print(f"uses_mock             = {uses_mock}")
    print(f"called_project_methods= {json.dumps(called_methods, ensure_ascii=False, indent=2)}")

    # 7. 输出有效代码注释文件
    sep("7. 输出有效代码注释文件")
    dump_effective_lines(
        TARGET_METHOD, target_method, TARGET_FILE,
        expanded_names, all_methods, all_methods_files,
        analyzer,
        output_dir=Path(__file__).parent / "debug",
        combined_import_map=combined_import_map,
        inheritance_field_map=inheritance_field_map,
    )


if __name__ == '__main__':
    main()
