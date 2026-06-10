#!/usr/bin/env python3
"""
批量运行 Maven 测试指标分析工具

多进程调用 Java 测试指标工具（jar），通过解析 Java 端 stderr 输出的进度协议
实时更新 rich 进度条，实现多项目并发进度展示。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

from utils.logger_manager import get_logger

logger = get_logger('logs/maven_test_metrics.log', 'TestMetrics')

# ---------------------------------------------------------------------------
# 全局状态（用于信号处理）
# ---------------------------------------------------------------------------
_active_processes: list[subprocess.Popen] = []
_shutdown_requested = False
_shutdown_lock = threading.Lock()
_completed_lock = threading.Lock()


def _signal_handler(signum: int, frame) -> None:
    """Ctrl+C 信号处理：杀掉所有子进程并立即退出。"""
    console = Console()
    console.print("\n[yellow]收到中断信号，正在停止所有子进程...[/yellow]")
    logger.warning("收到中断信号，强制停止")
    for proc in _active_processes:
        try:
            proc.kill()
        except Exception:
            pass
    os._exit(1)


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ---------------------------------------------------------------------------
# CLI 参数解析
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量运行 Maven 测试指标分析工具")
    parser.add_argument(
        "--projects", required=True, help="项目名称列表文件（每行一个，格式 owner/repo）"
    )
    parser.add_argument("--root", required=True, help="所有项目的根目录")
    parser.add_argument(
        "--output", default="test_metrics.csv", help="输出 CSV 文件路径"
    )
    parser.add_argument(
        "--workers", type=int, default=max(1, os.cpu_count() // 2),
        help="并行处理的线程数（默认 CPU 核数的一半）",
    )
    parser.add_argument(
        "--jar", default=None, help="Java jar 文件路径（默认自动检测）"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="单个项目超时时间（秒，默认 1800）",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# jar 路径自动检测
# ---------------------------------------------------------------------------
def resolve_jar_path(jar_arg: Optional[str]) -> str:
    if jar_arg:
        return jar_arg
    script_dir = Path(__file__).parent.resolve()
    default_jar = (
        script_dir / "target" / "maven-test-metrics-1.0-SNAPSHOT.jar"
    )
    if default_jar.exists():
        return str(default_jar)
    raise FileNotFoundError(
        f"默认 jar 不存在: {default_jar}\n"
        "请使用 --jar 指定 jar 路径，或先执行 mvn package 构建。"
    )


# ---------------------------------------------------------------------------
# 项目列表加载
# ---------------------------------------------------------------------------
def load_projects(projects_file: str) -> list[str]:
    path = Path(projects_file)
    if not path.exists():
        raise FileNotFoundError(f"项目列表文件不存在: {projects_file}")
    with path.open("r", encoding="utf-8") as f:
        projects = [line.strip() for line in f if line.strip()]
    logger.info(f"加载项目列表: {projects_file}, 共 {len(projects)} 个项目")
    return projects


# ---------------------------------------------------------------------------
# 工作目录与 completed.json 管理
# ---------------------------------------------------------------------------
def get_workdir(output_csv: str) -> Path:
    base = Path(output_csv).resolve()
    return base.parent / f"{base.stem}_workdir"


def load_completed(workdir: Path) -> set[str]:
    completed_file = workdir / "completed.json"
    if not completed_file.exists():
        return set()
    try:
        data = json.loads(completed_file.read_text(encoding="utf-8"))
        return set(data.get("completed", []))
    except Exception:
        return set()


def mark_completed(workdir: Path, project_name: str) -> None:
    completed_file = workdir / "completed.json"
    with _completed_lock:
        if completed_file.exists():
            try:
                data = json.loads(completed_file.read_text(encoding="utf-8"))
            except Exception:
                data = {"completed": []}
        else:
            data = {"completed": []}
        if project_name not in data["completed"]:
            data["completed"].append(project_name)
        tmp = completed_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.rename(completed_file)
        logger.info(f"标记项目完成: {project_name}")


def cleanup_incomplete_csvs(workdir: Path, completed: set[str]) -> int:
    if not workdir.exists():
        return 0
    removed = 0
    for csv_file in workdir.glob("*.csv"):
        safe_name = csv_file.stem
        project_name = safe_name.replace("_", "/")
        if project_name not in completed:
            try:
                csv_file.unlink()
                removed += 1
            except Exception:
                pass
    return removed


# ---------------------------------------------------------------------------
# 进度协议解析
# ---------------------------------------------------------------------------
class ProgressState:
    """单个 Java 子进程的进度状态。"""

    def __init__(self, project_name: str) -> None:
        self.project_name = project_name
        self.phase = "init"
        self.total = 0
        self.done = 0
        self.current_test = ""
        self.written = 0

    def parse_line(self, line: str) -> bool:
        """
        解析一行 stderr 输出。
        返回 True 表示成功解析到进度消息。
        """
        marker = "##PROGRESS## "
        if not line.startswith(marker):
            return False
        payload = line[len(marker) :].strip()
        parts = payload.split(None, 1)
        if not parts:
            return False
        cmd = parts[0]
        rest = parts[1] if len(parts) > 1 else ""

        if cmd == "PHASE":
            self.phase = rest
        elif cmd == "TEST_FILES":
            pass  # 仅记录，不在进度条中显示
        elif cmd == "TOTAL":
            try:
                self.total = int(rest)
            except ValueError:
                pass
        elif cmd == "DONE":
            # 格式: DONE <current_count>/<total_test_methods> <test_full_name>
            try:
                count_part, test_name = rest.split(" ", 1)
                self.done = int(count_part.split("/", 1)[0])
                self.current_test = test_name
            except ValueError:
                pass
        elif cmd == "WRITTEN":
            try:
                self.written = int(rest)
            except ValueError:
                pass
        return True


# ---------------------------------------------------------------------------
# 安全文件名生成
# ---------------------------------------------------------------------------
def safe_project_name(name: str) -> str:
    """将项目名中的特殊字符替换为下划线，用于文件名。"""
    return name.replace("/", "_").replace("\\", "_")


# ---------------------------------------------------------------------------
# 单项目分析（在独立线程中运行）
# ---------------------------------------------------------------------------
def analyze_project(
    jar_path: str,
    root_dir: str,
    project_name: str,
    workdir: Path,
    timeout: int,
    progress_ui: Progress,
    overall_task: TaskID,
) -> dict:
    """
    运行 Java 工具分析单个项目，实时更新 rich 进度条。
    返回结果字典，包含状态信息。
    """
    result = {
        "project": project_name,
        "status": "unknown",
        "test_count": 0,
        "message": "",
    }

    project_path = Path(root_dir) / project_name
    if not project_path.exists():
        result["status"] = "skipped"
        result["message"] = f"项目目录不存在: {project_path}"
        logger.warning(f"跳过项目 {project_name}: 目录不存在 {project_path}")
        progress_ui.advance(overall_task)
        return result

    csv_path = workdir / f"{safe_project_name(project_name)}.csv"

    # 优先使用 JAVA_HOME 下的 java
    java_home = os.environ.get('JAVA_HOME', '')
    java_bin = os.path.join(java_home, 'bin', 'java') if java_home else 'java'
    if java_home and not os.path.exists(java_bin):
        java_bin = 'java'

    cmd = [
        java_bin,
        "-jar",
        jar_path,
        "--root",
        str(project_path),
        "--name",
        project_name,
        "--output",
        str(csv_path),
        "--progress",
    ]

    # 创建 rich 子任务
    task_id = progress_ui.add_task(
        project_name[:30].ljust(30),
        total=1,
        completed=0,
    )
    state = ProgressState(project_name)
    start_time = time.time()
    logger.info(f"开始分析项目: {project_name}")

    proc: Optional[subprocess.Popen] = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        with _shutdown_lock:
            if not _shutdown_requested:
                _active_processes.append(proc)

        stderr_lines: list[str] = []

        # 实时读取 stderr 进度消息
        if proc.stderr is not None:
            for line in proc.stderr:
                line = line.strip()
                if not state.parse_line(line):
                    if line:
                        stderr_lines.append(line)
                    continue
                if state.total > 0:
                    progress_ui.update(
                        task_id,
                        total=state.total,
                        completed=state.done,
                        description=project_name[:30].ljust(30),
                    )
                if _shutdown_requested:
                    break

        # 等待进程结束（带超时）
        try:
            exit_code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            result["status"] = "timeout"
            stderr_tail = "\n".join(stderr_lines[-10:])
            result["message"] = f"超时（>{timeout}秒）"
            if stderr_tail:
                result["message"] += f"\n{stderr_tail}"
            logger.error(f"项目超时 {project_name}: >{timeout}秒")
            progress_ui.remove_task(task_id)
            progress_ui.advance(overall_task)
            return result

        if exit_code != 0:
            result["status"] = "failed"
            stderr_tail = "\n".join(stderr_lines[-10:])
            result["message"] = f"Java 进程退出码 {exit_code}"
            if stderr_tail:
                result["message"] += f"\n{stderr_tail}"
            logger.error(f"项目失败 {project_name}: 退出码 {exit_code}\n{stderr_tail}")
            progress_ui.remove_task(task_id)
            progress_ui.advance(overall_task)
            return result

        # 成功
        result["status"] = "success"
        result["test_count"] = state.written
        result["message"] = f"完成，共 {state.written} 条测试记录"
        progress_ui.remove_task(task_id)
        progress_ui.advance(overall_task)

        elapsed = time.time() - start_time
        logger.info(f"项目完成 {project_name}: {state.written} 条测试记录, 耗时 {elapsed:.1f}s")
        # 标记完成
        mark_completed(workdir, project_name)

    except Exception as e:
        result["status"] = "failed"
        result["message"] = str(e)
        logger.error(f"项目异常 {project_name}: {e}", exc_info=True)
        if task_id is not None:
            progress_ui.remove_task(task_id)
        progress_ui.advance(overall_task)
    finally:
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
            with _shutdown_lock:
                if proc in _active_processes:
                    _active_processes.remove(proc)

    return result


# ---------------------------------------------------------------------------
# CSV 合并
# ---------------------------------------------------------------------------
def merge_csv_files(
    output_csv: str,
    workdir: Path,
    console: Console,
) -> int:
    """
    合并所有已完成项目的 CSV 文件到最终输出文件。
    返回合并的总行数（不含 header）。
    """
    completed = load_completed(workdir)
    if not completed:
        return 0

    header = (
        "project_name,test_full_name,setup_length,assertion_count,"
        "mock_verify_count,uses_mock,called_project_methods,called_methods_count,"
        "called_packages,called_packages_count"
    )

    output_path = Path(output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total_rows = 0
    with output_path.open("w", encoding="utf-8", newline="") as out_f:
        out_f.write(header + "\n")
        for project_name in sorted(completed):
            csv_file = workdir / f"{safe_project_name(project_name)}.csv"
            if not csv_file.exists():
                continue
            with open(csv_file, "r", encoding="utf-8", newline="") as in_f:
                reader = csv.reader(in_f)
                first = True
                for row in reader:
                    if first:
                        # 跳过临时文件的 header
                        if row and row[0] == "project_name":
                            first = False
                            continue
                        first = False
                    if row:
                        out_f.write(",".join(_csv_escape_cell(c) for c in row) + "\n")
                        total_rows += 1

    return total_rows


def _csv_escape_cell(value: str) -> str:
    if value is None:
        return ""
    if "," in value or '"' in value or "\n" in value:
        return '"' + value.replace('"', '""') + '"'
    return value


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------
def main() -> int:
    args = parse_args()
    console = Console()

    # 1. 解析参数
    try:
        jar_path = resolve_jar_path(args.jar)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        return 1

    all_projects = load_projects(args.projects)
    if not all_projects:
        console.print("[yellow]项目列表为空。[/yellow]")
        return 0

    # 2. 创建工作目录
    workdir = get_workdir(args.output)
    workdir.mkdir(parents=True, exist_ok=True)

    # 3. 读取 completed.json → 已完成项目集合
    completed = load_completed(workdir)

    # 4. 清理工作目录中未完成项目的 CSV
    cleaned = cleanup_incomplete_csvs(workdir, completed)
    if cleaned > 0:
        console.print(f"[yellow]清理了 {cleaned} 个不完整的 CSV 文件[/yellow]")

    # 5. 过滤待处理项目列表（去掉已完成的）
    to_process = [p for p in all_projects if p not in completed]
    skipped_count = len(all_projects) - len(to_process)

    if skipped_count > 0:
        console.print(f"[cyan]断点续传：跳过 {skipped_count} 个已处理项目[/cyan]")

    if not to_process:
        console.print("[green]所有项目已处理完毕，无需运行。[/green]")
        # 仍然合并已完成的 CSV
        merged_rows = merge_csv_files(args.output, workdir, console)
        console.print(f"[green]合并 {merged_rows} 行到 {args.output}[/green]")
        return 0

    console.print(
        f"[bold]准备分析 {len(to_process)} 个项目，"
        f"使用 {args.workers} 个并发线程...[/bold]"
    )

    # 6. 运行并发分析
    results: list[dict] = []
    overall_start = time.time()

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=None),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress_ui:
        overall_task = progress_ui.add_task(
            "Overall",
            total=len(to_process),
            completed=0,
        )

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            future_to_project = {
                executor.submit(
                    analyze_project,
                    jar_path,
                    args.root,
                    project,
                    workdir,
                    args.timeout,
                    progress_ui,
                    overall_task,
                ): project
                for project in to_process
            }

            for future in as_completed(future_to_project):
                if _shutdown_requested:
                    break
                project = future_to_project[future]
                try:
                    result = future.result()
                except Exception as e:
                    result = {
                        "project": project,
                        "status": "failed",
                        "test_count": 0,
                        "message": str(e),
                    }
                results.append(result)

    # 7. 合并 CSV（覆盖写入，基于 completed.json）
    merged_rows = merge_csv_files(args.output, workdir, console)

    # 8. 统计输出
    total_time = time.time() - overall_start
    success_count = sum(1 for r in results if r["status"] == "success")
    fail_count = sum(1 for r in results if r["status"] == "failed")
    timeout_count = sum(1 for r in results if r["status"] == "timeout")
    skip_dir_count = sum(1 for r in results if r["status"] == "skipped")
    total_tests = sum(r["test_count"] for r in results if r["status"] == "success")

    console.print("\n" + "=" * 50)
    console.print("[bold]分析完成[/bold]")
    console.print(f"  总项目数  : {len(all_projects)}")
    if skipped_count:
        console.print(f"  已跳过    : {skipped_count}（已存在于 completed.json）")
    if skip_dir_count:
        console.print(f"  目录缺失  : {skip_dir_count}")
    console.print(f"  成功      : {success_count}")
    console.print(f"  失败      : {fail_count}")
    if timeout_count:
        console.print(f"  超时      : {timeout_count}")
    console.print(f"  总测试用例: {total_tests}")
    console.print(f"  总耗时    : {total_time:.1f} 秒")
    console.print(f"  合并行数  : {merged_rows}")
    console.print(f"  输出文件  : {args.output}")
    console.print(f"  工作目录  : {workdir}")
    console.print("=" * 50)

    # 显示失败详情
    failures = [r for r in results if r["status"] in ("failed", "timeout")]
    if failures:
        console.print("\n[red]失败项目详情：[/red]")
        for r in failures:
            console.print(f"  • {r['project']}: {r['message']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
