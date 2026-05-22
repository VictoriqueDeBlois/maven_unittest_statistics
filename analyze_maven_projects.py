#!/usr/bin/env python3
"""Batch statistics and classification for local Maven Java projects."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)


EXCLUDED_DIRS = {
    ".git",
    "target",
    "build",
    "out",
    ".idea",
    ".vscode",
    ".gradle",
    "node_modules",
    "dist",
    ".cache",
}

README_CANDIDATES = ["README.md", "README.MD", "README.rst", "README.txt", "README", "readme.md"]

CSV_FIELDS = [
    "owner",
    "repo",
    "project_path",
    "has_pom",
    "is_maven_project",
    "is_multi_module",
    "module_count",
    "pom_files_count",
    "packaging",
    "group_id",
    "artifact_id",
    "version",
    "repo_size_bytes",
    "total_files",
    "total_dirs",
    "java_files_count",
    "main_java_files",
    "test_java_files",
    "other_java_files",
    "main_loc",
    "test_loc",
    "other_loc",
    "total_loc",
    "main_sloc",
    "test_sloc",
    "other_sloc",
    "total_sloc",
    "dependency_count",
    "plugin_count",
    "profile_count",
    "has_readme",
    "readme_name",
    "top_packages",
    "top_level_dirs",
    "detected_signals",
    "type_label_zh",
    "llm_confidence",
    "llm_reason_zh",
    "llm_alternative_labels_zh",
    "estimated_compile_time_seconds",
    "compile_size_level",
    "compile_estimation_reason",
    "scan_status",
    "error_message",
]

CONSOLE = Console()

SYSTEM_PROMPT = """你是一个软件项目分类助手。你的任务是根据 Maven Java 项目的结构化摘要，判断该项目最合适的软件类型。

要求：
1. 输出必须是严格 JSON，不要输出 Markdown，不要输出解释性正文。
2. 项目类型标签必须使用中文。
3. type_label_zh 必须是一个简短中文短语，通常不要超过 8 个汉字；如果必须保留英文技术词，可以使用类似 “Web 后端”、“RPC 框架”、“Maven 插件” 的形式。
4. 不要使用完整句子作为标签。
5. 不要机械翻译 repo 名称，要根据 README、pom 描述、依赖、插件、目录结构、包名、模块名和启发式信号综合判断。
6. 项目类型可以有一定自由度，但应尽量使用常见软件类别。
7. 如果项目明显属于某个技术领域，优先给领域标签，例如“消息队列”“数据库”“搜索引擎”“日志框架”“RPC 框架”。
8. 如果项目是可复用代码，但没有明显业务领域，可以输出“通用工具库”。
9. 如果项目是示例、demo、sample、tutorial，输出“示例项目”。
10. 如果信息不足，输出“未知”，并降低 confidence。
11. reason_zh 只写一句中文理由。
12. alternative_labels_zh 给出 0 到 3 个备选中文标签。
"""

USER_PROMPT_TEMPLATE = """请根据下面的 Maven Java 项目摘要，判断该项目的类型。

你需要输出严格 JSON，格式如下：

{
  "type_label_zh": "中文短语",
  "confidence": 0.0,
  "reason_zh": "一句中文理由",
  "alternative_labels_zh": ["备选标签1", "备选标签2"]
}

分类要求：
- type_label_zh 必须是中文短语，例如：
  - Web 后端
  - 中间件
  - 游戏
  - 数据库工具
  - RPC 框架
  - 测试框架
  - 构建插件
  - 命令行工具
  - 静态分析工具
  - 示例项目
  - 通用工具库
- 不要输出太细碎的标签，例如不要输出“基于 Spring Boot 的用户管理后台系统”。
- 如果项目明显属于某个技术领域，优先给领域标签，例如“消息队列”“数据库”“搜索引擎”“日志框架”。
- 如果项目只是一些可复用代码，没有明显业务领域，可以输出“通用工具库”。
- 如果是示例、demo、tutorial、sample，输出“示例项目”。
- 如果信息不足，输出“未知”。

项目摘要如下：

{project_summary_json}
"""


@dataclass(frozen=True)
class ProjectRef:
    owner: str
    repo: str
    path: Path


@dataclass
class JavaLocStats:
    loc: int = 0
    sloc: int = 0


@dataclass
class FileStats:
    repo_size_bytes: int = 0
    total_files: int = 0
    total_dirs: int = 0
    java_files: list[Path] = field(default_factory=list)
    main_java_files: int = 0
    test_java_files: int = 0
    other_java_files: int = 0
    main_loc: int = 0
    test_loc: int = 0
    other_loc: int = 0
    main_sloc: int = 0
    test_sloc: int = 0
    other_sloc: int = 0


@dataclass
class PomInfo:
    packaging: str = ""
    group_id: str = ""
    artifact_id: str = ""
    version: str = ""
    name: str = ""
    description: str = ""
    modules: list[str] = field(default_factory=list)


@dataclass
class PomAggregate:
    pom_files_count: int = 0
    dependency_count: int = 0
    plugin_count: int = 0
    profile_count: int = 0
    dependencies: list[str] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)


@dataclass
class ReadmeInfo:
    has_readme: bool = False
    readme_name: str = ""
    excerpt: str = ""


@dataclass
class LlmClassification:
    type_label_zh: str = "未知"
    confidence: float = 0.0
    reason_zh: str = ""
    alternative_labels_zh: list[str] = field(default_factory=list)


@dataclass
class CompileEstimate:
    seconds: int = 0
    size_level: str = "unknown"
    reason: str = ""


def setup_logging(log_file: Path) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("maven_project_analyzer")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    file_formatter = logging.Formatter("[%(levelname)s] %(message)s")
    stream_handler = RichHandler(
        console=CONSOLE,
        show_time=False,
        show_path=False,
        markup=False,
        rich_tracebacks=True,
    )
    stream_handler.setFormatter(logging.Formatter("%(message)s"))
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(file_formatter)
    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)
    return logger


def create_progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=CONSOLE,
        transient=False,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze local owner/repo Maven Java projects.")
    parser.add_argument("--projects-root", required=True, type=Path)
    parser.add_argument("--output-csv", required=True, type=Path)
    parser.add_argument("--log-file", required=True, type=Path)
    parser.add_argument("--max-readme-chars", type=int, default=4000)
    parser.add_argument("--llm-batch-size", type=int, default=1)
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument(
        "--reuse-llm-csv",
        type=Path,
        default=None,
        help="Reuse type_label_zh/llm_* columns from a previous projects_stats.csv instead of calling the LLM.",
    )
    parser.add_argument(
        "--reuse-stats-csv",
        type=Path,
        default=None,
        help="Reuse full rows from a previous projects_stats.csv, only refreshing cheap project-path and Gradle metadata-POM flags.",
    )
    return parser.parse_args()


def discover_projects(projects_root: Path) -> list[ProjectRef]:
    projects: list[ProjectRef] = []
    for owner_dir in sorted(projects_root.iterdir()):
        if not owner_dir.is_dir():
            continue
        for repo_dir in sorted(owner_dir.iterdir()):
            if repo_dir.is_dir():
                projects.append(ProjectRef(owner=owner_dir.name, repo=repo_dir.name, path=repo_dir))
    return projects


def prune_dirs(dirnames: list[str]) -> None:
    dirnames[:] = [name for name in dirnames if name not in EXCLUDED_DIRS]


def is_main_java(path: Path) -> bool:
    return "src/main/java" in path.as_posix()


def is_test_java(path: Path) -> bool:
    return "src/test/java" in path.as_posix()


def scan_files(project_path: Path, logger: logging.Logger) -> FileStats:
    stats = FileStats()
    for root, dirs, files in os.walk(project_path):
        prune_dirs(dirs)
        stats.total_dirs += len(dirs)
        root_path = Path(root)
        for file_name in files:
            file_path = root_path / file_name
            stats.total_files += 1
            try:
                stats.repo_size_bytes += file_path.stat().st_size
            except OSError as exc:
                logger.warning("Failed to stat %s: %s", file_path, exc)
            if file_name.endswith(".java"):
                stats.java_files.append(file_path)
                loc_stats = count_java_loc(file_path)
                rel = file_path.relative_to(project_path)
                if is_main_java(rel):
                    stats.main_java_files += 1
                    stats.main_loc += loc_stats.loc
                    stats.main_sloc += loc_stats.sloc
                elif is_test_java(rel):
                    stats.test_java_files += 1
                    stats.test_loc += loc_stats.loc
                    stats.test_sloc += loc_stats.sloc
                else:
                    stats.other_java_files += 1
                    stats.other_loc += loc_stats.loc
                    stats.other_sloc += loc_stats.sloc
    return stats


def count_java_loc(java_file_path: Path) -> JavaLocStats:
    text = read_text_best_effort(java_file_path)
    if text == "":
        return JavaLocStats()
    loc = text.count("\n") + (0 if text.endswith("\n") else 1)
    return JavaLocStats(loc=loc, sloc=count_java_sloc(text))


def count_java_sloc(text: str) -> int:
    sloc = 0
    has_code = False
    state = "NORMAL"
    i = 0
    line_start = True

    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        nxt2 = text[i + 2] if i + 2 < len(text) else ""

        if ch == "\r":
            i += 1
            continue

        if ch == "\n":
            if has_code:
                sloc += 1
            has_code = False
            line_start = True
            if state == "LINE_COMMENT":
                state = "NORMAL"
            i += 1
            continue

        if state == "NORMAL":
            if ch.isspace():
                i += 1
                continue
            if ch == "/" and nxt == "/":
                state = "LINE_COMMENT"
                i += 2
                continue
            if ch == "/" and nxt == "*":
                state = "BLOCK_COMMENT"
                i += 2
                continue
            has_code = True
            if ch == '"' and nxt == '"' and nxt2 == '"':
                state = "TEXT_BLOCK"
                i += 3
                line_start = False
                continue
            if ch == '"':
                state = "STRING_LITERAL"
            elif ch == "'":
                state = "CHAR_LITERAL"
            line_start = False
            i += 1
            continue

        if state == "LINE_COMMENT":
            i += 1
            continue

        if state == "BLOCK_COMMENT":
            if ch == "*" and nxt == "/":
                state = "NORMAL"
                i += 2
            else:
                i += 1
            continue

        if state == "STRING_LITERAL":
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                state = "NORMAL"
            i += 1
            continue

        if state == "CHAR_LITERAL":
            if ch == "\\":
                i += 2
                continue
            if ch == "'":
                state = "NORMAL"
            i += 1
            continue

        if state == "TEXT_BLOCK":
            if ch == '"' and nxt == '"' and nxt2 == '"':
                state = "NORMAL"
                i += 3
            else:
                i += 1
            continue

        i += 1

    if has_code:
        sloc += 1
    return sloc


def read_text_best_effort(path: Path, max_chars: int | None = None) -> str:
    for encoding in ("utf-8", "latin-1"):
        try:
            text = path.read_text(encoding=encoding, errors="strict")
            return text[:max_chars] if max_chars is not None else text
        except UnicodeDecodeError:
            continue
        except OSError:
            return ""
    return ""


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child_text(parent: ET.Element | None, name: str) -> str:
    if parent is None:
        return ""
    for child in parent:
        if local_name(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def find_children(parent: ET.Element, path_names: tuple[str, ...]) -> list[ET.Element]:
    current = [parent]
    for name in path_names:
        next_nodes: list[ET.Element] = []
        for node in current:
            next_nodes.extend(child for child in node if local_name(child.tag) == name)
        current = next_nodes
    return current


def parse_xml(path: Path) -> ET.Element:
    return ET.parse(path).getroot()


def parse_root_pom(project_path: Path) -> PomInfo:
    root = parse_xml(project_path / "pom.xml")
    parent = next((child for child in root if local_name(child.tag) == "parent"), None)
    modules = [module.text.strip() for module in find_children(root, ("modules", "module")) if module.text]
    return PomInfo(
        packaging=child_text(root, "packaging") or "jar",
        group_id=child_text(root, "groupId") or child_text(parent, "groupId"),
        artifact_id=child_text(root, "artifactId"),
        version=child_text(root, "version") or child_text(parent, "version"),
        name=child_text(root, "name"),
        description=child_text(root, "description"),
        modules=modules,
    )


def has_root_gradle_build(project_path: Path) -> bool:
    gradle_files = (
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
    )
    return any((project_path / name).is_file() for name in gradle_files)


def is_gradle_metadata_pom(project_path: Path) -> bool:
    pom_path = project_path / "pom.xml"
    text = read_text_best_effort(pom_path, 12000).lower()
    markers = (
        "published-with-gradle-metadata",
        "gradle module metadata",
        "should prefer consuming it instead",
    )
    return any(marker in text for marker in markers)


def looks_like_gradle_primary_project_fast(project_path: Path, row: dict[str, Any] | None = None) -> bool:
    if not (project_path / "pom.xml").is_file() or not has_root_gradle_build(project_path):
        return False
    if is_gradle_metadata_pom(project_path):
        return True

    row = row or {}
    try:
        module_count = int(row.get("module_count") or 0)
        plugin_count = int(row.get("plugin_count") or 0)
        pom_files_count = int(row.get("pom_files_count") or 0)
    except (TypeError, ValueError):
        return False

    has_gradle_wrapper = (project_path / "gradlew").is_file() or (project_path / "gradlew.bat").is_file()
    has_settings = (project_path / "settings.gradle").is_file() or (project_path / "settings.gradle.kts").is_file()
    return has_gradle_wrapper and has_settings and module_count == 0 and plugin_count == 0 and pom_files_count <= 1


def looks_like_gradle_primary_project(
    project_path: Path,
    pom_info: PomInfo,
    pom_aggregate: PomAggregate,
) -> bool:
    if not (project_path / "pom.xml").is_file() or not has_root_gradle_build(project_path):
        return False
    if is_gradle_metadata_pom(project_path):
        return True

    has_gradle_wrapper = (project_path / "gradlew").is_file() or (project_path / "gradlew.bat").is_file()
    has_settings = (project_path / "settings.gradle").is_file() or (project_path / "settings.gradle.kts").is_file()
    root_pom_has_no_build_logic = (
        not pom_info.modules
        and pom_aggregate.plugin_count == 0
        and pom_aggregate.pom_files_count == 1
    )
    return has_gradle_wrapper and has_settings and root_pom_has_no_build_logic


def iter_pom_files(project_path: Path) -> list[Path]:
    pom_files: list[Path] = []
    for root, dirs, files in os.walk(project_path):
        prune_dirs(dirs)
        if "pom.xml" in files:
            pom_files.append(Path(root) / "pom.xml")
    return pom_files


def parse_all_poms(project_path: Path, logger: logging.Logger) -> PomAggregate:
    dependencies: set[str] = set()
    plugins: set[str] = set()
    profile_count = 0
    pom_files = iter_pom_files(project_path)
    for pom_file in pom_files:
        try:
            root = parse_xml(pom_file)
        except ET.ParseError as exc:
            logger.warning("POM 解析失败 %s: %s", pom_file, exc)
            continue
        for dep in root.iter():
            if local_name(dep.tag) != "dependency":
                continue
            group_id = child_text(dep, "groupId")
            artifact_id = child_text(dep, "artifactId")
            if artifact_id:
                dependencies.add(f"{group_id}:{artifact_id}" if group_id else artifact_id)
        for plugin in root.iter():
            if local_name(plugin.tag) != "plugin":
                continue
            artifact_id = child_text(plugin, "artifactId")
            group_id = child_text(plugin, "groupId")
            if artifact_id:
                plugins.add(f"{group_id}:{artifact_id}" if group_id else artifact_id)
        profile_count += sum(1 for node in root.iter() if local_name(node.tag) == "profile")
    return PomAggregate(
        pom_files_count=len(pom_files),
        dependency_count=len(dependencies),
        plugin_count=len(plugins),
        profile_count=profile_count,
        dependencies=sorted(dependencies),
        plugins=sorted(plugins),
    )


def extract_readme(project_path: Path, max_chars: int, logger: logging.Logger) -> ReadmeInfo:
    for name in README_CANDIDATES:
        path = project_path / name
        if path.is_file():
            text = read_text_best_effort(path, max_chars)
            if text == "":
                logger.warning("README 读取失败 %s", path)
            return ReadmeInfo(has_readme=True, readme_name=name, excerpt=text)
    return ReadmeInfo()


def extract_top_level_dirs(project_path: Path) -> list[str]:
    return sorted(
        item.name
        for item in project_path.iterdir()
        if item.is_dir() and item.name not in EXCLUDED_DIRS
    )


PACKAGE_RE = re.compile(r"^\s*package\s+([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)\s*;", re.MULTILINE)


def extract_top_packages(project_path: Path) -> list[str]:
    counter: Counter[str] = Counter()
    main_root = project_path / "src" / "main" / "java"
    if not main_root.exists():
        return []
    for java_file in main_root.rglob("*.java"):
        if any(part in EXCLUDED_DIRS for part in java_file.parts):
            continue
        text = read_text_best_effort(java_file, 8000)
        match = PACKAGE_RE.search(text)
        if not match:
            continue
        parts = match.group(1).split(".")
        package = ".".join(parts[:3] if len(parts) >= 3 else parts)
        counter[package] += 1
    return [package for package, _ in counter.most_common(10)]


def detect_signals(
    repo: str,
    readme_excerpt: str,
    dependencies: list[str],
    plugins: list[str],
    top_level_dirs: list[str],
    top_packages: list[str],
) -> list[str]:
    blob = " ".join([repo, readme_excerpt, *dependencies, *plugins, *top_level_dirs, *top_packages]).lower()
    checks = [
        ("spring_boot", ("spring-boot", "spring boot")),
        ("spring", ("spring-", "org.springframework", "springframework")),
        ("servlet", ("servlet", "jakarta.servlet", "javax.servlet")),
        ("maven_plugin", ("maven-plugin", "maven plugin", "maven-plugin-plugin")),
        ("junit", ("junit", "jupiter")),
        ("mockito", ("mockito",)),
        ("testng", ("testng",)),
        ("netty", ("netty",)),
        ("grpc", ("grpc",)),
        ("jdbc", ("jdbc",)),
        ("mybatis", ("mybatis",)),
        ("hibernate", ("hibernate",)),
        ("database", ("database", "datasource", "postgres", "mysql", "sqlite", "oracle", "mariadb")),
        ("redis", ("redis",)),
        ("kafka", ("kafka",)),
        ("rabbitmq", ("rabbitmq", "amqp")),
        ("rocketmq", ("rocketmq",)),
        ("elasticsearch", ("elasticsearch", "elastic-search")),
        ("lucene", ("lucene",)),
        ("hadoop", ("hadoop",)),
        ("spark", ("spark",)),
        ("flink", ("flink",)),
        ("android", ("android",)),
        ("game", ("game", "gaming")),
        ("cli", ("cli", "command-line", "command line")),
        ("annotation_processor", ("annotation-processor", "processor", "apt", "auto-service")),
        ("code_generation", ("codegen", "code-gen", "generate", "generator")),
        ("static_analysis", ("checkstyle", "pmd", "spotbugs", "error-prone", "static analysis")),
        ("logging", ("logging", "log4j", "slf4j", "logback")),
        ("security", ("security", "crypto", "oauth", "jwt")),
        ("web", ("web", "http", "rest", "spring-boot-starter-web")),
        ("rpc", ("rpc", "dubbo", "grpc", "thrift")),
        ("scala", ("scala-maven-plugin", "scala-library")),
        ("kotlin", ("kotlin-maven-plugin", "kotlin-stdlib")),
        ("protobuf", ("protobuf", "proto", "protoc")),
    ]
    signals = [signal for signal, keywords in checks if any(keyword in blob for keyword in keywords)]
    return sorted(dict.fromkeys(signals))


def build_project_summary(
    ref: ProjectRef,
    pom_info: PomInfo,
    pom_aggregate: PomAggregate,
    readme: ReadmeInfo,
    file_stats: FileStats,
    top_level_dirs: list[str],
    top_packages: list[str],
    signals: list[str],
) -> dict[str, Any]:
    return {
        "owner": ref.owner,
        "repo": ref.repo,
        "pom_name": pom_info.name,
        "pom_description": pom_info.description,
        "readme_excerpt": readme.excerpt,
        "packaging": pom_info.packaging,
        "group_id": pom_info.group_id,
        "artifact_id": pom_info.artifact_id,
        "module_count": len(pom_info.modules),
        "pom_files_count": pom_aggregate.pom_files_count,
        "main_java_files": file_stats.main_java_files,
        "main_sloc": file_stats.main_sloc,
        "test_java_files": file_stats.test_java_files,
        "test_sloc": file_stats.test_sloc,
        "dependencies": pom_aggregate.dependencies[:80],
        "plugins": pom_aggregate.plugins[:50],
        "top_level_dirs": top_level_dirs[:50],
        "top_packages": top_packages[:10],
        "detected_signals": signals,
    }


def unknown_classification(reason: str = "大模型分类失败") -> LlmClassification:
    return LlmClassification(type_label_zh="未知", confidence=0.0, reason_zh=reason, alternative_labels_zh=[])


def load_reused_classifications(path: Path, logger: logging.Logger) -> dict[str, LlmClassification]:
    reused: dict[str, LlmClassification] = {}
    if not path.exists():
        raise FileNotFoundError(f"reuse LLM CSV not found: {path}")

    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            owner = str(row.get("owner") or "").strip()
            repo = str(row.get("repo") or "").strip()
            if not owner or not repo:
                project_name = str(row.get("project_name") or "").strip()
                if "/" in project_name:
                    owner, repo = project_name.split("/", 1)
            if not owner or not repo:
                continue

            alternatives = [
                item.strip()
                for item in str(row.get("llm_alternative_labels_zh") or "").split(";")
                if item.strip()
            ]
            try:
                confidence = float(row.get("llm_confidence") or 0)
            except (TypeError, ValueError):
                confidence = 0.0
            reused[f"{owner}/{repo}"] = LlmClassification(
                type_label_zh=str(row.get("type_label_zh") or "未知").strip() or "未知",
                confidence=confidence,
                reason_zh=str(row.get("llm_reason_zh") or "").strip(),
                alternative_labels_zh=alternatives[:3],
            )

    logger.info("Loaded %s reused LLM classifications from %s", len(reused), path)
    return reused


def load_reused_rows(path: Path, logger: logging.Logger) -> dict[str, dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"reuse stats CSV not found: {path}")

    reused: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for source_row in reader:
            row = {field: source_row.get(field, "") for field in CSV_FIELDS}
            owner = str(row.get("owner") or "").strip()
            repo = str(row.get("repo") or "").strip()
            if not owner or not repo:
                continue
            reused[f"{owner}/{repo}"] = row

    logger.info("Loaded %s reused project-stat rows from %s", len(reused), path)
    return reused


def reuse_project_row(ref: ProjectRef, previous_row: dict[str, Any]) -> dict[str, Any]:
    row = {field: previous_row.get(field, "") for field in CSV_FIELDS}
    row.update(owner=ref.owner, repo=ref.repo, project_path=str(ref.path))

    error_parts = [
        part.strip()
        for part in str(row.get("error_message") or "").split(";")
        if part.strip() and part.strip() != "excluded as gradle primary project with metadata pom"
    ]
    if looks_like_gradle_primary_project_fast(ref.path, row):
        row["has_pom"] = bool((ref.path / "pom.xml").is_file())
        row["is_maven_project"] = False
        error_parts.append("excluded as gradle primary project with metadata pom")
    row["error_message"] = "; ".join(error_parts)
    return row


def classify_project_with_llm(summary: dict[str, Any], logger: logging.Logger) -> LlmClassification:
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL")
    if not api_key or not model:
        raise RuntimeError("OPENAI_API_KEY and OPENAI_MODEL are required unless --skip-llm is set")

    from openai import OpenAI

    client = OpenAI(
        api_key=api_key,
        base_url=os.getenv("OPENAI_BASE_URL") or None,
        timeout=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "60")),
    )
    max_retries = int(os.getenv("OPENAI_MAX_RETRIES", "3"))
    temperature = float(os.getenv("OPENAI_TEMPERATURE", "0.2"))
    prompt = USER_PROMPT_TEMPLATE.replace(
        "{project_summary_json}", json.dumps(summary, ensure_ascii=False, indent=2)
    )

    attempts = max(1, max_retries)
    for attempt in range(1, attempts + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=temperature,
            )
            content = response.choices[0].message.content or ""
            classification, should_retry = validate_llm_response(content)
            if should_retry and attempt < attempts:
                logger.warning("LLM 标签过长，重试一次: %s", classification.type_label_zh)
                continue
            return classification
        except Exception as exc:
            logger.error("LLM 调用失败 attempt=%s/%s: %s", attempt, attempts, exc)
    return unknown_classification()


def validate_llm_response(content: str) -> tuple[LlmClassification, bool]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        raise ValueError("LLM JSON 解析失败")

    missing = {"type_label_zh", "confidence", "reason_zh", "alternative_labels_zh"} - data.keys()
    if missing:
        raise ValueError(f"LLM JSON 缺少字段: {sorted(missing)}")

    label = str(data.get("type_label_zh") or "").replace("\n", "").strip()
    label = label.strip("。.:：\"'“”‘’ ")
    if not label:
        label = "未知"

    try:
        confidence = float(data.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    if not 0 <= confidence <= 1:
        confidence = 0.0

    alternatives = data.get("alternative_labels_zh")
    if not isinstance(alternatives, list):
        alternatives = []
    alternatives = [str(item).strip() for item in alternatives[:3] if str(item).strip()]

    return (
        LlmClassification(
            type_label_zh=label,
            confidence=confidence,
            reason_zh=str(data.get("reason_zh") or "").strip(),
            alternative_labels_zh=alternatives,
        ),
        len(label) > 20,
    )


def estimate_compile_time(
    main_sloc: int,
    main_java_files: int,
    module_count: int,
    dependency_count: int,
    plugin_count: int,
    pom_files_count: int,
    signals: list[str],
) -> CompileEstimate:
    if main_sloc <= 0 or main_java_files <= 0:
        return CompileEstimate(0, "unknown", "未发现 src/main/java 主代码")

    estimated = (
        10
        + main_sloc / 500
        + main_java_files * 0.03
        + module_count * 8
        + dependency_count * 0.4
        + plugin_count * 0.8
    )
    multipliers: list[str] = []
    signal_set = set(signals)
    if "annotation_processor" in signal_set:
        estimated *= 1.25
        multipliers.append("annotation processor")
    if "code_generation" in signal_set:
        estimated *= 1.30
        multipliers.append("code generation")
    if "scala" in signal_set or "kotlin" in signal_set:
        estimated *= 1.40
        multipliers.append("Scala/Kotlin")
    if "spring_boot" in signal_set:
        estimated *= 1.10
        multipliers.append("Spring Boot")
    if "grpc" in signal_set:
        estimated *= 1.10
        multipliers.append("gRPC")
    if "protobuf" in signal_set:
        estimated *= 1.25
        multipliers.append("protobuf/codegen")

    seconds = int(round(max(10, min(7200, estimated))))
    if seconds < 30:
        level = "tiny"
    elif seconds < 120:
        level = "small"
    elif seconds < 300:
        level = "medium"
    elif seconds < 900:
        level = "large"
    else:
        level = "huge"
    multiplier_text = f"，存在 {'、'.join(multipliers)}" if multipliers else ""
    reason = (
        f"主代码约 {main_sloc} SLOC，{module_count} 个模块，{dependency_count} 个依赖，"
        f"{plugin_count} 个插件，{pom_files_count} 个 POM{multiplier_text}，因此估计为 {level}。"
    )
    return CompileEstimate(seconds=seconds, size_level=level, reason=reason)


def build_base_row(ref: ProjectRef) -> dict[str, Any]:
    return {field: "" for field in CSV_FIELDS} | {
        "owner": ref.owner,
        "repo": ref.repo,
        "project_path": str(ref.path),
        "has_pom": False,
        "is_maven_project": False,
        "is_multi_module": False,
        "module_count": 0,
        "pom_files_count": 0,
        "repo_size_bytes": 0,
        "total_files": 0,
        "total_dirs": 0,
        "java_files_count": 0,
        "main_java_files": 0,
        "test_java_files": 0,
        "other_java_files": 0,
        "main_loc": 0,
        "test_loc": 0,
        "other_loc": 0,
        "total_loc": 0,
        "main_sloc": 0,
        "test_sloc": 0,
        "other_sloc": 0,
        "total_sloc": 0,
        "dependency_count": 0,
        "plugin_count": 0,
        "profile_count": 0,
        "has_readme": False,
        "type_label_zh": "未知",
        "llm_confidence": 0,
        "llm_reason_zh": "",
        "llm_alternative_labels_zh": "",
        "estimated_compile_time_seconds": 0,
        "compile_size_level": "unknown",
        "compile_estimation_reason": "",
        "scan_status": "ok",
        "error_message": "",
    }


def analyze_project(
    ref: ProjectRef,
    max_readme_chars: int,
    skip_llm: bool,
    reused_classifications: dict[str, LlmClassification] | None,
    logger: logging.Logger,
) -> dict[str, Any]:
    row = build_base_row(ref)
    error_messages: list[str] = []
    try:
        file_stats = scan_files(ref.path, logger)
        row.update(
            repo_size_bytes=file_stats.repo_size_bytes,
            total_files=file_stats.total_files,
            total_dirs=file_stats.total_dirs,
            java_files_count=len(file_stats.java_files),
            main_java_files=file_stats.main_java_files,
            test_java_files=file_stats.test_java_files,
            other_java_files=file_stats.other_java_files,
            main_loc=file_stats.main_loc,
            test_loc=file_stats.test_loc,
            other_loc=file_stats.other_loc,
            total_loc=file_stats.main_loc + file_stats.test_loc + file_stats.other_loc,
            main_sloc=file_stats.main_sloc,
            test_sloc=file_stats.test_sloc,
            other_sloc=file_stats.other_sloc,
            total_sloc=file_stats.main_sloc + file_stats.test_sloc + file_stats.other_sloc,
        )

        pom_info = PomInfo()
        if (ref.path / "pom.xml").is_file():
            row["has_pom"] = True
            row["is_maven_project"] = True
            try:
                pom_info = parse_root_pom(ref.path)
                row.update(
                    is_multi_module=len(pom_info.modules) > 0,
                    module_count=len(pom_info.modules),
                    packaging=pom_info.packaging,
                    group_id=pom_info.group_id,
                    artifact_id=pom_info.artifact_id,
                    version=pom_info.version,
                )
            except Exception as exc:
                logger.warning("POM 解析失败 %s: %s", ref.path / "pom.xml", exc)
                error_messages.append(f"pom parse failed: {short_error(exc)}")

        pom_aggregate = parse_all_poms(ref.path, logger)
        row.update(
            pom_files_count=pom_aggregate.pom_files_count,
            dependency_count=pom_aggregate.dependency_count,
            plugin_count=pom_aggregate.plugin_count,
            profile_count=pom_aggregate.profile_count,
        )
        if looks_like_gradle_primary_project(ref.path, pom_info, pom_aggregate):
            row["is_maven_project"] = False
            error_messages.append("excluded as gradle primary project with metadata pom")

        readme = extract_readme(ref.path, max_readme_chars, logger)
        row.update(has_readme=readme.has_readme, readme_name=readme.readme_name)

        top_level_dirs = extract_top_level_dirs(ref.path)
        top_packages = extract_top_packages(ref.path)
        signals = detect_signals(
            ref.repo,
            readme.excerpt,
            pom_aggregate.dependencies,
            pom_aggregate.plugins,
            top_level_dirs,
            top_packages,
        )
        row.update(
            top_level_dirs=";".join(top_level_dirs),
            top_packages=";".join(top_packages),
            detected_signals=";".join(signals),
        )

        summary = build_project_summary(
            ref, pom_info, pom_aggregate, readme, file_stats, top_level_dirs, top_packages, signals
        )
        cache_key = f"{ref.owner}/{ref.repo}"
        if reused_classifications and cache_key in reused_classifications:
            classification = reused_classifications[cache_key]
        elif skip_llm:
            classification = unknown_classification("已跳过大模型分类")
        else:
            try:
                classification = classify_project_with_llm(summary, logger)
            except Exception as exc:
                logger.error("LLM classification failed for %s/%s: %s", ref.owner, ref.repo, exc)
                error_messages.append(f"llm failed: {short_error(exc)}")
                classification = unknown_classification()
        row.update(
            type_label_zh=classification.type_label_zh,
            llm_confidence=classification.confidence,
            llm_reason_zh=classification.reason_zh,
            llm_alternative_labels_zh=";".join(classification.alternative_labels_zh),
        )

        estimate = estimate_compile_time(
            file_stats.main_sloc,
            file_stats.main_java_files,
            len(pom_info.modules),
            pom_aggregate.dependency_count,
            pom_aggregate.plugin_count,
            pom_aggregate.pom_files_count,
            signals,
        )
        row.update(
            estimated_compile_time_seconds=estimate.seconds,
            compile_size_level=estimate.size_level,
            compile_estimation_reason=estimate.reason,
        )
        row["error_message"] = "; ".join(error_messages)
    except Exception as exc:
        logger.exception("Project scan failed for %s/%s", ref.owner, ref.repo)
        row["scan_status"] = "failed"
        row["error_message"] = short_error(exc)
    return row


def short_error(exc: BaseException) -> str:
    return str(exc).replace("\n", " ")[:500]


def write_csv(rows: list[dict[str, Any]], output_csv: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def validate_environment(skip_llm: bool) -> None:
    load_dotenv()
    if skip_llm:
        return
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    model = (os.getenv("OPENAI_MODEL") or "").strip()
    placeholder_keys = {"your_api_key_here", "your-api-key-here", "sk-your-api-key"}
    if not api_key or not model:
        raise SystemExit("OPENAI_API_KEY and OPENAI_MODEL are required unless --skip-llm is set")
    if api_key.lower() in placeholder_keys:
        raise SystemExit("OPENAI_API_KEY is still a placeholder in .env; set a real key or use --skip-llm")


def main() -> None:
    args = parse_args()
    logger = setup_logging(args.log_file)
    started = time.time()
    validate_environment(args.skip_llm or args.reuse_llm_csv is not None)
    if args.llm_batch_size != 1:
        logger.warning("--llm-batch-size 第一版固定按 1 处理，当前传入值会被忽略: %s", args.llm_batch_size)
    reused_classifications = (
        load_reused_classifications(args.reuse_llm_csv, logger) if args.reuse_llm_csv else None
    )
    reused_rows = load_reused_rows(args.reuse_stats_csv, logger) if args.reuse_stats_csv else None

    logger.info("开始扫描 %s", args.projects_root)
    projects = discover_projects(args.projects_root)
    logger.info("Found %s candidate projects.", len(projects))

    rows: list[dict[str, Any]] = []
    with create_progress() as progress:
        task_id = progress.add_task("Analyzing projects", total=len(projects))
        for ref in projects:
            progress.update(task_id, description=f"Analyzing {ref.owner}/{ref.repo}")
            cache_key = f"{ref.owner}/{ref.repo}"
            if reused_rows and cache_key in reused_rows:
                logger.info("Reusing previous stats for %s/%s", ref.owner, ref.repo)
                row = reuse_project_row(ref, reused_rows[cache_key])
            else:
                logger.info("Processing %s/%s", ref.owner, ref.repo)
                row = analyze_project(ref, args.max_readme_chars, args.skip_llm, reused_classifications, logger)
            rows.append(row)
            logger.info(
                "Finished %s/%s: status=%s, label=%s, estimated_compile_time=%s",
                ref.owner,
                ref.repo,
                row["scan_status"],
                row["type_label_zh"],
                row["estimated_compile_time_seconds"],
            )
            progress.advance(task_id)

    write_csv(rows, args.output_csv)
    logger.info("CSV 写入完成: %s", args.output_csv)
    logger.info("总耗时 %.2f 秒", time.time() - started)


if __name__ == "__main__":
    main()
