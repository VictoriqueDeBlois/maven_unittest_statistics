#!/usr/bin/env python3
"""
Build a balanced integration-test benchmark from test and project statistics.

The selector is intentionally deterministic:
  1. exclude mock-based tests
  2. exclude projects with unclear or incomplete metadata
  3. merge similar project type labels into broader domains
  4. assign difficulty from project scale and called_packages_count
  5. sample by difficulty, domain, and project to keep the result diverse
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter, defaultdict
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import matplotlib.pyplot as plt
from matplotlib import font_manager

import extract_test_snippets
from select_complex_integration_tests import match_integration_keyword


UNCLEAR_TYPE_LABELS = {
    "",
    "unknown",
    "Unknown",
    "UNKNOWN",
    "未知",
    "未知项目",
    "无法判断",
    "不明确",
    "N/A",
    "nan",
    "None",
    "示例项目",
}

SCALE_SCORE = {
    "tiny": 1,
    "small": 2,
    "medium": 3,
    "large": 4,
    "huge": 5,
}

SCALE_ORDER = ["tiny", "small", "medium", "large", "huge"]
DIFFICULTY_ORDER = ["easy", "medium", "hard", "expert"]
DIFFICULTY_PICK_ORDER = ["expert", "hard", "medium", "easy"]
DIFFICULTY_QUOTAS = {
    "easy": 0.20,
    "medium": 0.35,
    "hard": 0.30,
    "expert": 0.15,
}

DOMAIN_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("安全与风控", ("安全", "漏洞", "渗透", "认证", "授权", "加密", "风控", "Burp")),
    ("云服务与SDK", ("云", "SDK", "OpenAPI")),
    ("分布式与中间件", ("分布式", "消息", "队列", "RPC", "微服务", "服务治理", "注册中心", "事务", "网格", "网关")),
    ("数据处理与计算", ("流处理", "大数据", "ETL", "计算", "规则引擎", "工作流", "流程", "任务调度")),
    ("数据库与存储", ("数据库", "ORM", "JDBC", "SQL", "存储", "缓存", "Redis", "搜索", "索引", "图数据库")),
    ("Web与业务系统", ("Web", "博客", "内容", "CMS", "电商", "后台", "管理系统", "门户", "论坛", "支付")),
    ("开发与构建工具", ("开发工具", "构建", "Maven", "Gradle", "插件", "脚手架", "代码生成", "IDE", "CLI", "命令行")),
    ("测试与质量", ("测试", "Mock", "质量", "覆盖率", "压测", "基准")),
    ("网络与协议", ("网络", "HTTP", "客户端", "服务器", "协议", "Netty", "通信")),
    ("文档与媒体", ("PDF", "文档", "报表", "图像", "音频", "视频", "GIS", "地图")),
    ("游戏与图形", ("游戏", "图形", "引擎")),
]


def normalize_domain(type_label: str, alternative_labels: str = "", signals: str = "") -> str:
    # The LLM label is a better semantic signal than the raw detected-signals
    # bag, so use signals only as a fallback.
    text = f"{type_label};{alternative_labels}"
    for domain, keywords in DOMAIN_RULES:
        if any(keyword.lower() in text.lower() for keyword in keywords):
            return domain
    for domain, keywords in DOMAIN_RULES:
        if any(keyword.lower() in signals.lower() for keyword in keywords):
            return domain
    return "其他明确领域"


def package_score(called_packages_count: int) -> int:
    if called_packages_count >= 15:
        return 5
    if called_packages_count >= 10:
        return 4
    if called_packages_count >= 7:
        return 3
    if called_packages_count >= 5:
        return 2
    return 1


def difficulty_from(scale: str, called_packages_count: int) -> tuple[str, int]:
    score = SCALE_SCORE.get(str(scale).strip().lower(), 0) + package_score(called_packages_count)
    if score <= 4:
        return "easy", score
    if score <= 6:
        return "medium", score
    if score <= 8:
        return "hard", score
    return "expert", score


def _read_projects(projects_csv: Path, commit_times_csv: Path | None) -> pd.DataFrame:
    projects = pd.read_csv(projects_csv, dtype=str).fillna("")
    projects["project_name"] = projects["owner"].str.strip() + "/" + projects["repo"].str.strip()

    numeric_cols = [
        "estimated_compile_time_seconds",
        "total_sloc",
        "main_sloc",
        "main_java_files",
        "test_sloc",
        "dependency_count",
        "module_count",
        "pom_files_count",
        "llm_confidence",
    ]
    for col in numeric_cols:
        if col in projects.columns:
            projects[col] = pd.to_numeric(projects[col], errors="coerce")

    if commit_times_csv and commit_times_csv.exists():
        commits = pd.read_csv(commit_times_csv, dtype=str).fillna("")
        keep = ["project_name", "commit_hash", "commit_time_iso", "commit_time_readable"]
        projects = projects.merge(commits[keep], on="project_name", how="left")
    else:
        projects["commit_hash"] = ""
        projects["commit_time_iso"] = ""
        projects["commit_time_readable"] = ""

    return projects


def _is_truthy(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def _looks_like_gradle_metadata_pom(project_path: str) -> bool:
    path = Path(str(project_path))
    if not path.exists():
        return False

    pom_path = path / "pom.xml"
    if not pom_path.is_file():
        return False

    has_root_gradle = any(
        (path / name).is_file()
        for name in ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")
    )
    if not has_root_gradle:
        return False

    try:
        text = pom_path.read_text(encoding="utf-8", errors="ignore")[:12000].lower()
    except OSError:
        return False

    markers = (
        "published-with-gradle-metadata",
        "gradle module metadata",
        "should prefer consuming it instead",
    )
    return any(marker in text for marker in markers)


def _valid_projects(projects: pd.DataFrame, min_llm_confidence: float) -> pd.DataFrame:
    type_label = projects.get("type_label_zh", pd.Series("", index=projects.index)).astype(str).str.strip()
    scale = projects.get("compile_size_level", pd.Series("", index=projects.index)).astype(str).str.strip().str.lower()
    has_pom = projects.get("has_pom", pd.Series(False, index=projects.index)).map(_is_truthy)
    is_maven_project = projects.get("is_maven_project", pd.Series(False, index=projects.index)).map(_is_truthy)
    main_java_files = projects.get("main_java_files", pd.Series(0, index=projects.index)).fillna(0)
    main_sloc = projects.get("main_sloc", pd.Series(0, index=projects.index)).fillna(0)
    gradle_metadata_pom = projects.get("project_path", pd.Series("", index=projects.index)).map(_looks_like_gradle_metadata_pom)

    mask = (
        (projects.get("scan_status", "ok").astype(str).str.lower() == "ok")
        & has_pom
        & is_maven_project
        & ~gradle_metadata_pom
        & type_label.notna()
        & ~type_label.isin(UNCLEAR_TYPE_LABELS)
        & scale.isin(SCALE_SCORE)
        & projects["estimated_compile_time_seconds"].notna()
        & (projects["estimated_compile_time_seconds"] > 0)
        & main_java_files.notna()
        & main_sloc.notna()
        & (main_java_files > 0)
        & (main_sloc > 0)
    )

    if "llm_confidence" in projects.columns:
        mask &= projects["llm_confidence"].fillna(0) >= min_llm_confidence

    valid = projects.loc[mask].copy()
    valid["project_domain"] = valid.apply(
        lambda row: normalize_domain(
            str(row.get("type_label_zh", "")),
            str(row.get("llm_alternative_labels_zh", "")),
            str(row.get("detected_signals", "")),
        ),
        axis=1,
    )
    return valid


def _add_keyword_columns(rows: pd.DataFrame) -> pd.DataFrame:
    has_keyword = []
    matched_keyword = []
    for test_name in rows["test_full_name"].astype(str):
        is_hit, keyword = match_integration_keyword(test_name)
        has_keyword.append("true" if is_hit else "false")
        matched_keyword.append(keyword or "")
    rows["has_keyword"] = has_keyword
    rows["matched_keyword"] = matched_keyword
    return rows


def _sort_candidates(rows: pd.DataFrame) -> pd.DataFrame:
    for col in ["called_packages_count", "called_methods_count", "setup_length", "assertion_count"]:
        rows[col] = pd.to_numeric(rows[col], errors="coerce").fillna(0).astype(int)
    return rows.sort_values(
        by=["called_packages_count", "called_methods_count", "setup_length", "assertion_count", "project_name", "test_full_name"],
        ascending=[False, False, False, False, True, True],
    )


def _read_project_exclude_file(path: Path | None) -> set[str]:
    if not path:
        return set()
    projects: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        projects.update(item.strip() for item in re.split(r"[,;\s]+", line) if item.strip())
    return projects


def _find_duplicate_tests(tests: pd.DataFrame) -> pd.DataFrame:
    duplicate_rows = tests[
        tests.duplicated(["project_name", "test_full_name"], keep=False)
    ].copy()
    if duplicate_rows.empty:
        return duplicate_rows

    duplicate_summary = (
        duplicate_rows.groupby(["project_name", "test_full_name"], dropna=False)
        .size()
        .reset_index(name="duplicate_count")
        .sort_values(["project_name", "test_full_name"])
    )
    return duplicate_summary


def _write_exclusion_log(
    output_dir: Path,
    duplicate_summary: pd.DataFrame,
    manual_excluded_projects: set[str],
    repair_dropped_projects: set[str] | None = None,
) -> None:
    lines = []
    duplicate_projects = (
        sorted(set(duplicate_summary["project_name"].astype(str)))
        if not duplicate_summary.empty
        else []
    )

    lines.append(f"duplicate_test_project_count={len(duplicate_projects)}")
    lines.append(f"duplicate_test_full_name_count={len(duplicate_summary)}")
    lines.append(f"duplicate_test_rows_removed={int(duplicate_summary['duplicate_count'].sum()) if not duplicate_summary.empty else 0}")
    lines.append(f"manual_excluded_project_count={len(manual_excluded_projects)}")
    if repair_dropped_projects is not None:
        lines.append(f"repair_dropped_project_count={len(repair_dropped_projects)}")
    lines.append("")

    if duplicate_projects:
        lines.append("[removed_duplicate_test_full_names]")
        for project in duplicate_projects:
            project_dups = duplicate_summary[duplicate_summary["project_name"] == project]
            examples = [
                f"{row.test_full_name} x{row.duplicate_count}"
                for row in project_dups.head(5).itertuples(index=False)
            ]
            suffix = f"; examples: {' | '.join(examples)}" if examples else ""
            removed_rows = int(project_dups["duplicate_count"].sum())
            lines.append(f"{project}: duplicate_test_names={len(project_dups)}, rows_removed={removed_rows}{suffix}")
        lines.append("")

    if manual_excluded_projects:
        lines.append("[manual_excluded_projects]")
        lines.extend(sorted(manual_excluded_projects))
        lines.append("")

    if repair_dropped_projects:
        lines.append("[repair_dropped_projects]")
        lines.extend(sorted(repair_dropped_projects))
        lines.append("")

    (output_dir / "excluded_projects.log").write_text("\n".join(lines), encoding="utf-8")


def _project_order(series: pd.Series) -> list[str]:
    return list(dict.fromkeys(series.astype(str).tolist()))


def _balanced_pick(
    rows: pd.DataFrame,
    quota: int,
    global_project_counts: Counter[str],
    max_per_project: int,
) -> list[dict[str, object]]:
    by_domain_project: dict[str, dict[str, list[dict[str, object]]]] = defaultdict(lambda: defaultdict(list))

    for row in rows.to_dict("records"):
        by_domain_project[str(row["project_domain"])][str(row["project_name"])].append(row)

    picked: list[dict[str, object]] = []
    domain_counts: Counter[str] = Counter()
    local_project_counts: Counter[str] = Counter()

    while len(picked) < quota:
        progress = False
        domains = sorted(
            by_domain_project,
            key=lambda d: (domain_counts[d], -sum(len(v) for v in by_domain_project[d].values()), d),
        )
        for domain in domains:
            projects = by_domain_project[domain]
            available_projects = [
                project
                for project, project_rows in projects.items()
                if project_rows and global_project_counts[project] < max_per_project
            ]
            if not available_projects:
                continue

            project = min(
                available_projects,
                key=lambda p: (
                    0 if local_project_counts[p] > 0 else 1,
                    -local_project_counts[p],
                    global_project_counts[p],
                    -len(projects[p]),
                    p,
                ),
            )
            row = projects[project].pop(0)
            picked.append(row)
            domain_counts[domain] += 1
            local_project_counts[project] += 1
            global_project_counts[project] += 1
            progress = True

            if len(picked) >= quota:
                break

        if not progress:
            break

    return picked


def _write_charts(selected_df: pd.DataFrame, output_dir: Path) -> None:
    _configure_chart_font()
    chart_dir = output_dir / "charts"
    chart_dir.mkdir(parents=True, exist_ok=True)

    chart_specs = [
        ("difficulty", "Difficulty distribution", "difficulty_distribution.png"),
        ("project_domain", "Domain distribution", "domain_distribution.png"),
        ("project_compile_size_level", "Project size distribution", "project_size_distribution.png"),
    ]

    for column, title, filename in chart_specs:
        counts = selected_df[column].value_counts()
        if column == "difficulty":
            counts = counts.reindex(DIFFICULTY_ORDER).dropna()
        elif column == "project_compile_size_level":
            counts = counts.reindex(SCALE_ORDER).dropna()

        plt.figure(figsize=(10, 5))
        counts.plot(kind="bar", color="#3b82f6")
        plt.title(title)
        plt.ylabel("test count")
        plt.xlabel("")
        plt.xticks(rotation=35, ha="right")
        plt.tight_layout()
        plt.savefig(chart_dir / filename, dpi=160)
        plt.close()

    domain_scale = pd.crosstab(selected_df["project_domain"], selected_df["project_compile_size_level"])
    domain_scale = domain_scale.reindex(columns=[s for s in SCALE_ORDER if s in domain_scale.columns])
    plt.figure(figsize=(11, 6))
    domain_scale.plot(kind="bar", stacked=True, ax=plt.gca())
    plt.title("Domain by project size")
    plt.ylabel("test count")
    plt.xlabel("")
    plt.xticks(rotation=35, ha="right")
    plt.tight_layout()
    plt.savefig(chart_dir / "domain_by_size.png", dpi=160)
    plt.close()

    per_project_counts = selected_df["project_name"].value_counts().value_counts().sort_index()
    plt.figure(figsize=(8, 5))
    per_project_counts.plot(kind="bar", color="#10b981")
    plt.title("Tests per project")
    plt.ylabel("project count")
    plt.xlabel("tests selected per project")
    plt.xticks(rotation=0)
    plt.tight_layout()
    plt.savefig(chart_dir / "tests_per_project.png", dpi=160)
    plt.close()

    project_size_counts = (
        selected_df.drop_duplicates("project_name")["project_compile_size_level"]
        .value_counts()
        .reindex(SCALE_ORDER)
        .dropna()
    )
    plt.figure(figsize=(8, 5))
    project_size_counts.plot(kind="bar", color="#6366f1")
    plt.title("Project count by size")
    plt.ylabel("project count")
    plt.xlabel("")
    plt.xticks(rotation=0)
    plt.tight_layout()
    plt.savefig(chart_dir / "project_count_by_size.png", dpi=160)
    plt.close()


def _configure_chart_font() -> None:
    font_paths = [
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        "/usr/share/fonts/truetype/unifont/unifont.ttf",
    ]
    for font_path in font_paths:
        if Path(font_path).exists():
            font_manager.fontManager.addfont(font_path)
            font_name = font_manager.FontProperties(fname=font_path).get_name()
            plt.rcParams["font.family"] = font_name
            plt.rcParams["axes.unicode_minus"] = False
            return


def _difficulty_quotas(target_count: int) -> dict[str, int]:
    quotas = {level: int(target_count * ratio) for level, ratio in DIFFICULTY_QUOTAS.items()}
    remainder = target_count - sum(quotas.values())
    for level in ["medium", "hard", "easy", "expert"]:
        if remainder <= 0:
            break
        quotas[level] += 1
        remainder -= 1
    return quotas


def _select_representative_projects(
    candidates: pd.DataFrame,
    target_projects: int,
    tests_per_project: int,
    max_huge_projects: int,
    initial_size_counts: Counter[str] | None = None,
) -> list[str]:
    project_rows = []
    for (project_name, domain, size), group in candidates.groupby(
        ["project_name", "project_domain", "project_compile_size_level"],
        dropna=False,
    ):
        ranked = group.sort_values(
            by=["difficulty_score", "called_packages_count", "called_methods_count", "setup_length", "test_full_name"],
            ascending=[False, False, False, False, True],
        )
        preview = ranked.head(tests_per_project)
        project_rows.append(
            {
                "project_name": project_name,
                "project_domain": domain,
                "project_compile_size_level": size,
                "candidate_count": len(group),
                "preview_easy_count": int((preview["difficulty"] == "easy").sum()),
                "preview_hard_count": int(preview["difficulty"].isin(["hard", "expert"]).sum()),
                "preview_avg_difficulty_score": float(preview["difficulty_score"].mean()),
                "max_difficulty_score": int(group["difficulty_score"].max()),
                "max_called_packages_count": int(group["called_packages_count"].max()),
                "avg_called_packages_count": float(group["called_packages_count"].mean()),
                "estimated_compile_time_seconds": float(group["estimated_compile_time_seconds"].iloc[0]),
            }
        )

    project_stats = pd.DataFrame(project_rows)
    project_stats = project_stats[project_stats["candidate_count"] >= tests_per_project].copy()
    project_stats = project_stats.sort_values(
        by=[
            "preview_easy_count",
            "preview_hard_count",
            "preview_avg_difficulty_score",
            "max_difficulty_score",
            "max_called_packages_count",
            "candidate_count",
            "estimated_compile_time_seconds",
            "project_name",
        ],
        ascending=[True, False, False, False, False, False, True, True],
    )

    cells: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in project_stats.to_dict("records"):
        cell = (str(row["project_domain"]), str(row["project_compile_size_level"]))
        cells[cell].append(str(row["project_name"]))

    selected: list[str] = []
    selected_set: set[str] = set()
    cell_counts: Counter[tuple[str, str]] = Counter()
    size_counts: Counter[str] = Counter(initial_size_counts or {})

    while len(selected) < target_projects:
        progress = False
        ordered_cells = sorted(
            cells,
            key=lambda cell: (
                cell_counts[cell],
                -(SCALE_ORDER.index(cell[1]) if cell[1] in SCALE_ORDER else -1),
                cell[0],
            ),
        )
        for cell in ordered_cells:
            while cells[cell] and cells[cell][0] in selected_set:
                cells[cell].pop(0)
            if not cells[cell]:
                continue
            project = cells[cell].pop(0)
            if cell[1] == "huge" and size_counts["huge"] >= max_huge_projects:
                continue
            selected.append(project)
            selected_set.add(project)
            cell_counts[cell] += 1
            size_counts[cell[1]] += 1
            progress = True
            if len(selected) >= target_projects:
                break
        if not progress:
            break

    return selected


def _pick_tests_for_projects(
    candidates: pd.DataFrame,
    projects: list[str],
    tests_per_project: int,
) -> pd.DataFrame:
    selected_groups = []
    for project in projects:
        group = candidates[candidates["project_name"] == project].copy()
        group = group.sort_values(
            by=["difficulty_score", "called_packages_count", "called_methods_count", "setup_length", "test_full_name"],
            ascending=[False, False, False, False, True],
        ).head(tests_per_project)
        selected_groups.append(group)
    if not selected_groups:
        return candidates.head(0).copy()
    return pd.concat(selected_groups, ignore_index=True)


def _prepare_candidates(
    all_tests_csv: Path,
    valid_projects: pd.DataFrame,
    min_packages: int,
    excluded_projects: set[str],
) -> tuple[pd.DataFrame, pd.DataFrame, set[str]]:
    tests = pd.read_csv(all_tests_csv)
    duplicate_summary = _find_duplicate_tests(tests)
    if not duplicate_summary.empty:
        duplicate_index = pd.MultiIndex.from_frame(duplicate_summary[["project_name", "test_full_name"]])
        tests_index = pd.MultiIndex.from_frame(tests[["project_name", "test_full_name"]])
        tests = tests[~tests_index.isin(duplicate_index)].copy()
    excluded_projects = set(excluded_projects)

    tests["called_packages_count"] = pd.to_numeric(tests["called_packages_count"], errors="coerce").fillna(0).astype(int)
    tests["uses_mock_norm"] = tests["uses_mock"].astype(str).str.strip().str.lower()
    tests = tests[
        (tests["uses_mock_norm"] != "true")
        & (tests["called_packages_count"] >= min_packages)
        & ~tests["project_name"].astype(str).isin(excluded_projects)
    ].copy()
    tests = tests.drop(columns=["uses_mock_norm"])

    project_cols = [
        "project_name",
        "type_label_zh",
        "project_domain",
        "compile_size_level",
        "estimated_compile_time_seconds",
        "total_sloc",
        "commit_hash",
        "commit_time_iso",
        "commit_time_readable",
    ]
    candidates = tests.merge(valid_projects[project_cols], on="project_name", how="inner")
    candidates = candidates.rename(
        columns={
            "type_label_zh": "project_type_label_zh",
            "compile_size_level": "project_compile_size_level",
            "total_sloc": "project_total_sloc",
        }
    )

    difficulty = candidates.apply(
        lambda row: difficulty_from(row["project_compile_size_level"], int(row["called_packages_count"])),
        axis=1,
    )
    candidates["difficulty"] = [item[0] for item in difficulty]
    candidates["difficulty_score"] = [item[1] for item in difficulty]
    candidates = _add_keyword_columns(_sort_candidates(candidates))
    return candidates, duplicate_summary, excluded_projects


def _repair_existing_selection(
    existing_csv: Path,
    candidates: pd.DataFrame,
    excluded_projects: set[str],
    target_projects: int,
    tests_per_project: int,
    max_huge_projects: int,
) -> tuple[pd.DataFrame, set[str]]:
    existing = pd.read_csv(existing_csv)
    candidate_projects = set(candidates["project_name"].astype(str))
    original_projects = set(existing["project_name"].astype(str))
    keep_mask = (
        ~existing["project_name"].astype(str).isin(excluded_projects)
        & existing["project_name"].astype(str).isin(candidate_projects)
    )
    kept = existing[keep_mask].copy()
    dropped_projects = original_projects - set(kept["project_name"].astype(str))

    kept_projects = set(kept["project_name"].astype(str))
    initial_size_counts = Counter(
        kept.drop_duplicates("project_name")["project_compile_size_level"].astype(str)
    )
    remaining_project_slots = max(0, target_projects - len(kept_projects))

    if remaining_project_slots > 0:
        remaining_candidates = candidates[
            ~candidates["project_name"].astype(str).isin(kept_projects | excluded_projects)
        ].copy()
        supplement_projects = _select_representative_projects(
            remaining_candidates,
            remaining_project_slots,
            tests_per_project,
            max_huge_projects=max_huge_projects,
            initial_size_counts=initial_size_counts,
        )
        supplement = _pick_tests_for_projects(remaining_candidates, supplement_projects, tests_per_project)
        if not supplement.empty:
            kept = pd.concat([kept, supplement], ignore_index=True)

    return kept, dropped_projects


def build_balanced_benchmark(
    all_tests_csv: Path,
    projects_csv: Path,
    commit_times_csv: Path,
    output_dir: Path,
    exclude_projects_file: Path | None = None,
    repair_from_csv: Path | None = None,
    target_count: int | None = None,
    target_projects: int = 50,
    tests_per_project: int = 5,
    min_count: int = 200,
    max_count: int = 400,
    min_packages: int = 5,
    max_per_project: int = 8,
    min_llm_confidence: float = 0.70,
    max_compile_time_seconds: int = 1800,
    max_huge_projects: int = 6,
    excluded_size_levels: set[str] | None = None,
) -> pd.DataFrame:
    if target_count is not None and not (min_count <= target_count <= max_count):
        raise ValueError(f"target_count must be between {min_count} and {max_count}")
    if not (min_count <= target_projects * tests_per_project <= max_count):
        raise ValueError(
            "target_projects * tests_per_project should be within the requested range "
            f"({min_count}-{max_count}); got {target_projects * tests_per_project}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    manual_excluded_projects = _read_project_exclude_file(exclude_projects_file)

    projects = _read_projects(projects_csv, commit_times_csv)
    valid_projects = _valid_projects(projects, min_llm_confidence)
    excluded_size_levels = excluded_size_levels or set()
    valid_projects = valid_projects[
        ~valid_projects["compile_size_level"].astype(str).str.lower().isin(excluded_size_levels)
        & (valid_projects["estimated_compile_time_seconds"] <= max_compile_time_seconds)
    ].copy()

    candidates, duplicate_summary, excluded_projects = _prepare_candidates(
        all_tests_csv,
        valid_projects,
        min_packages,
        manual_excluded_projects,
    )

    repair_dropped_projects: set[str] | None = None
    if repair_from_csv:
        selected_df, repair_dropped_projects = _repair_existing_selection(
            repair_from_csv,
            candidates,
            excluded_projects,
            target_projects,
            tests_per_project,
            max_huge_projects,
        )
    else:
        selected_projects = _select_representative_projects(
            candidates,
            target_projects,
            tests_per_project,
            max_huge_projects=max_huge_projects,
        )
        selected_df = _pick_tests_for_projects(candidates, selected_projects, tests_per_project)

    if len(selected_df) > max_count:
        selected_df = selected_df.head(max_count)

    if len(selected_df) < min_count:
        raise RuntimeError(f"Only selected {len(selected_df)} tests, below required minimum {min_count}")

    selected_df = selected_df.sort_values(
        by=["difficulty_score", "called_packages_count", "project_domain", "project_name", "test_full_name"],
        ascending=[True, True, True, True, True],
    )
    _write_exclusion_log(output_dir, duplicate_summary, manual_excluded_projects, repair_dropped_projects)

    selected_csv = output_dir / "balanced_tests.csv"
    selected_df.to_csv(selected_csv, index=False, quoting=csv.QUOTE_MINIMAL)

    for level in DIFFICULTY_ORDER:
        selected_df[selected_df["difficulty"] == level].to_csv(
            output_dir / f"balanced_tests_{level}.csv",
            index=False,
            quoting=csv.QUOTE_MINIMAL,
        )

    project_summary = (
        selected_df.groupby(
            [
                "project_name",
                "project_type_label_zh",
                "project_domain",
                "project_compile_size_level",
                "estimated_compile_time_seconds",
                "project_total_sloc",
                "commit_hash",
                "commit_time_iso",
                "commit_time_readable",
            ],
            dropna=False,
        )
        .agg(
            selected_test_count=("test_full_name", "count"),
            max_called_packages_count=("called_packages_count", "max"),
            avg_called_packages_count=("called_packages_count", "mean"),
            difficulties=("difficulty", lambda s: ";".join(sorted(set(s), key=DIFFICULTY_ORDER.index))),
        )
        .reset_index()
    )
    project_summary.to_csv(output_dir / "selected_projects.csv", index=False, quoting=csv.QUOTE_MINIMAL)

    summary_lines = [
        f"selected_tests={len(selected_df)}",
        f"selected_projects={selected_df['project_name'].nunique()}",
        f"selected_domains={selected_df['project_domain'].nunique()}",
        f"min_packages={min_packages}",
        f"exclude_mock=true",
        f"duplicate_test_projects_seen={duplicate_summary['project_name'].nunique() if not duplicate_summary.empty else 0}",
        f"duplicate_test_full_names_removed={len(duplicate_summary)}",
        f"duplicate_test_rows_removed={int(duplicate_summary['duplicate_count'].sum()) if not duplicate_summary.empty else 0}",
        f"manual_excluded_projects={len(manual_excluded_projects)}",
        f"repair_from_csv={repair_from_csv or ''}",
        f"target_projects={target_projects}",
        f"tests_per_project={tests_per_project}",
        f"max_compile_time_seconds={max_compile_time_seconds}",
        f"max_huge_projects={max_huge_projects}",
        f"excluded_size_levels={';'.join(sorted(excluded_size_levels))}",
        "",
        "[difficulty]",
    ]
    summary_lines.extend(f"{k}={v}" for k, v in selected_df["difficulty"].value_counts().sort_index().items())
    summary_lines.append("")
    summary_lines.append("[domain]")
    summary_lines.extend(f"{k}={v}" for k, v in selected_df["project_domain"].value_counts().items())
    summary_lines.append("")
    summary_lines.append("[project_compile_size_level]")
    summary_lines.extend(f"{k}={v}" for k, v in selected_df["project_compile_size_level"].value_counts().items())
    summary_lines.append("")
    summary_lines.append("[tests_per_project]")
    summary_lines.extend(f"{k}={v}" for k, v in selected_df["project_name"].value_counts().value_counts().sort_index().items())
    (output_dir / "summary.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    _write_charts(selected_df, output_dir)

    print(f"Selected tests: {len(selected_df)}")
    print(f"Selected projects: {selected_df['project_name'].nunique()}")
    print(f"Selected domains: {selected_df['project_domain'].nunique()}")
    print(f"Output dir: {output_dir}")
    return selected_df


def extract_outputs(output_dir: Path, csv_file: Path, projects_root: Path, jar_path: Path, workers: int) -> None:
    for fmt, subdir in [("annotated", "benchmark_annotated"), ("raw-java", "benchmark_raw_java")]:
        args = [
            "--csv",
            str(csv_file),
            "--root",
            str(projects_root),
            "--output",
            str(output_dir / subdir),
            "--mode",
            "all",
            "--workers",
            str(workers),
            "--jar",
            str(jar_path),
            "--format",
            fmt,
        ]
        with patch("sys.argv", ["extract_test_snippets.py"] + args):
            extract_test_snippets.main()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a balanced 200-400 test benchmark")
    parser.add_argument("--tests", default="all_tests_jar.csv", help="Input all-tests CSV")
    parser.add_argument("--projects", default="projects_stats.csv", help="Input project statistics CSV")
    parser.add_argument("--commits", default="repo_commit_times.csv", help="Input repo commit time CSV")
    parser.add_argument("--output-dir", default="balanced_benchmark", help="Output directory")
    parser.add_argument(
        "--exclude-projects",
        type=Path,
        default=None,
        help="Text file listing owner/repo projects to exclude; supports whitespace, comma, semicolon, and # comments.",
    )
    parser.add_argument(
        "--repair-from-csv",
        type=Path,
        default=None,
        help="Existing selected benchmark CSV to keep where possible, dropping excluded projects and supplementing replacements.",
    )
    parser.add_argument("--target-count", type=int, default=None, help="Deprecated: exact test target is no longer used by default")
    parser.add_argument("--target-projects", type=int, default=50, help="Target representative project count")
    parser.add_argument("--tests-per-project", type=int, default=5, help="Tests selected from each representative project")
    parser.add_argument("--min-count", type=int, default=200, help="Minimum selected test count")
    parser.add_argument("--max-count", type=int, default=400, help="Maximum selected test count")
    parser.add_argument("--min-packages", type=int, default=5, help="Minimum called_packages_count")
    parser.add_argument("--max-per-project", type=int, default=8, help="Maximum tests per project")
    parser.add_argument("--min-llm-confidence", type=float, default=0.70, help="Minimum project type confidence")
    parser.add_argument("--max-compile-time-seconds", type=int, default=1800, help="Exclude projects above this estimated Maven compile time")
    parser.add_argument("--max-huge-projects", type=int, default=6, help="Maximum huge projects to include")
    parser.add_argument(
        "--exclude-size-level",
        action="append",
        default=[],
        help="Exclude compile_size_level value; can be repeated.",
    )
    parser.add_argument("--extract-code", action="store_true", help="Extract annotated and raw Java files")
    parser.add_argument("--root", default="/data/xuhaoran/github", help="Projects root for code extraction")
    parser.add_argument(
        "--jar",
        default="/data/xuhaoran/idea/maven-test-metrics-java/target/maven-test-metrics-1.0-SNAPSHOT.jar",
        help="maven-test-metrics jar path for code extraction",
    )
    parser.add_argument("--workers", type=int, default=20, help="Extraction workers")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    selected = build_balanced_benchmark(
        all_tests_csv=Path(args.tests),
        projects_csv=Path(args.projects),
        commit_times_csv=Path(args.commits),
        output_dir=output_dir,
        exclude_projects_file=args.exclude_projects,
        repair_from_csv=args.repair_from_csv,
        target_count=args.target_count,
        target_projects=args.target_projects,
        tests_per_project=args.tests_per_project,
        min_count=args.min_count,
        max_count=args.max_count,
        min_packages=args.min_packages,
        max_per_project=args.max_per_project,
        min_llm_confidence=args.min_llm_confidence,
        max_compile_time_seconds=args.max_compile_time_seconds,
        max_huge_projects=args.max_huge_projects,
        excluded_size_levels={s.lower() for s in args.exclude_size_level},
    )

    if args.extract_code:
        extract_outputs(
            output_dir=output_dir,
            csv_file=output_dir / "balanced_tests.csv",
            projects_root=Path(args.root),
            jar_path=Path(args.jar),
            workers=args.workers,
        )

    return 0 if len(selected) >= args.min_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
