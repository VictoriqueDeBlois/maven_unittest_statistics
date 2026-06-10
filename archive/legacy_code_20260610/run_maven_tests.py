#!/usr/bin/env python3
"""
Maven 测试批量运行脚本
- 先 git reset 恢复干净状态，再运行 Maven 测试
- 使用 JaCoCo agent 动态注入收集覆盖率
- 并行处理，实时写入 CSV
"""

import argparse
import csv
import os
import subprocess
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd

from utils.logger_manager import get_logger


class MavenTester:
    # ── CSV 字段 ──────────────────────────────────────────────────────────────────
    CSV_FIELDS = [
        'project',
        'test_success',
        'jacoco_found',
        'instruction_coverage',
        'branch_coverage',
        'line_coverage',
        'complexity_coverage',
        'method_coverage',
        'notes',
    ]

    csv_lock = threading.Lock()

    # ── 环境配置（按需修改） ───────────────────────────────────────────────────────
    JAVA_HOME      = '/data/xuhaoran/program/jdk1.8.0_421'
    MAVEN_HOME     = '/data/xuhaoran/program/apache-maven-3.9.8'
    M2_HOME        = '/data/xuhaoran/.m2'
    JACOCO_VERSION = '0.8.12'
    JACOCO_CLI_JAR = '/data/xuhaoran/program/jacoco/lib/jacococli.jar'

    def __init__(self, log_dir: Path):
        self.logger = get_logger(log_dir / 'runner.log', 'MavenTester')


    def get_env(self) -> dict:
        env = os.environ.copy()
        env['JAVA_HOME'] = self.JAVA_HOME
        env['CLASSPATH'] = f".:{self.JAVA_HOME}/lib/dt.jar:{self.JAVA_HOME}/lib/tools.jar"
        env['PATH'] = f"{self.JAVA_HOME}/bin:{self.MAVEN_HOME}/bin:{env['PATH']}"
        env['MAVEN_HOME'] = self.MAVEN_HOME
        return env


    # ── 日志 ──────────────────────────────────────────────────────────────────────
    def append_log(self, log_file: Path, header: str, text: str):
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, 'a', encoding='utf-8', errors='replace') as f:
            f.write(f"\n{'='*60}\n{header}\n{'='*60}\n{text}\n")


    # ── Git 操作 ──────────────────────────────────────────────────────────────────
    def git_reset(self, project_path: Path, git_log_file: Path) -> bool:
        """git reset --hard && git clean -fd，日志单独写入 git_log_file"""
        env = self.get_env()
        ok = True
        for cmd in [['git', 'reset', '--hard'], ['git', 'clean', '-fd']]:
            result = subprocess.run(
                cmd,
                cwd=str(project_path),
                capture_output=True,
                text=True,
                env=env,
            )
            output = result.stdout + result.stderr
            self.append_log(git_log_file, f"$ {' '.join(cmd)}", output)
            if result.returncode != 0:
                ok = False
        return ok


    # ── Maven target 目录 ─────────────────────────────────────────────────────────
    def get_target_directory(self, project_path: Path) -> str:
        env = self.get_env()
        try:
            result = subprocess.run(
                ['mvn', 'help:evaluate', '-Dexpression=project.build.directory', '-q', '-DforceStdout'],
                cwd=str(project_path),
                capture_output=True,
                text=True,
                env=env,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass
        # fallback
        return str(project_path / 'target')


    # ── Maven 测试 ────────────────────────────────────────────────────────────────
    def run_maven_test(self, project_path: Path, target_path: str, mvn_log_file: Path) -> bool:
        """使用 JaCoCo agent 运行 mvn test，stdout/stderr 写入 mvn_log_file"""
        env = self.get_env()
        agent_jar = os.path.join(
            self.M2_HOME, 'repository', 'org', 'jacoco', 'org.jacoco.agent',
            self.JACOCO_VERSION,
            f'org.jacoco.agent-{self.JACOCO_VERSION}-runtime.jar',
        )
        jacoco_exec = os.path.join(target_path, 'jacoco.exec')
        arg_line = f'-javaagent:{agent_jar}=destfile={jacoco_exec}'

        cmd = [
            'mvn', 'clean', 'test',
            '-B',
            '-Dmaven.test.failure.ignore=false',
            f'-DargLine={arg_line}',
        ]

        mvn_log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(mvn_log_file, 'a', encoding='utf-8', errors='replace') as f:
            f.write(f"\n{'='*60}\n$ {' '.join(cmd)}\n{'='*60}\n")
            f.flush()
            proc = subprocess.Popen(
                cmd,
                cwd=str(project_path),
                stdout=f,
                stderr=subprocess.STDOUT,
                env=env,
            )
            proc.wait()

        return proc.returncode == 0


    # ── JaCoCo 报告 ───────────────────────────────────────────────────────────────
    def get_modules(self, pom_file: str) -> list:
        try:
            tree = ET.parse(pom_file)
            root = tree.getroot()
            ns = {'mvn': 'http://maven.apache.org/POM/4.0.0'}
            modules = root.findall('.//mvn:modules/mvn:module', ns)
            return [m.text for m in modules if m.text]
        except Exception:
            return []


    def report_jacoco_module(
            self,
            project_path: Path,
            jacoco_exec: str,
            module: str,
            env: dict,
    ) -> list:
        """
        递归对每个模块生成 CSV 报告，返回所有生成的 CSV 路径列表。
        module 为相对于 project_path 的路径，根模块传空字符串 ''。
        """
        output_dirs = []

        module_abs = project_path / module if module else project_path
        pom_file = str(module_abs / 'pom.xml')
        sub_modules = self.get_modules(pom_file)
        for sub in sub_modules:
            sub_rel = os.path.join(module, sub) if module else sub
            output_dirs.extend(self.report_jacoco_module(project_path, jacoco_exec, sub_rel, env))

        class_files = str(module_abs / 'target/classes')
        source_files = str(module_abs / 'src/main/java')

        if not os.path.exists(class_files):
            return output_dirs

        report_name = module.replace('/', '_') if module else 'root'
        output_csv = str(project_path / 'target' / f'{report_name}.csv')
        os.makedirs(os.path.dirname(output_csv), exist_ok=True)

        cmd = [
            'java', '-jar', self.JACOCO_CLI_JAR, 'report', jacoco_exec,
            '--classfiles', class_files,
            '--sourcefiles', source_files,
            '--csv', output_csv,
            '--name', report_name,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, env=env)
            output_dirs.append(output_csv)
        except subprocess.CalledProcessError as e:
            self.logger.warning(f"jacoco report 失败 [{report_name}]: {e.stderr}")

        return output_dirs


    def collect_jacoco_coverage(self, project_path: Path) -> dict:
        """生成 JaCoCo 报告并计算覆盖率，返回结果字典"""
        env = self.get_env()

        # 查找 jacoco.exec（优先项目根 target，再递归搜索）
        default_exec = project_path / 'target' / 'jacoco.exec'
        if default_exec.exists():
            jacoco_exec = str(default_exec)
        else:
            found = list(project_path.rglob('jacoco.exec'))
            if not found:
                return {'jacoco_found': False, 'notes': 'jacoco.exec 未找到'}
            jacoco_exec = str(found[0])

        csv_files = self.report_jacoco_module(project_path, jacoco_exec, '', env)
        if not csv_files:
            return {'jacoco_found': False, 'notes': '未生成任何 jacoco CSV 报告'}

        dfs = []
        for f in csv_files:
            try:
                dfs.append(pd.read_csv(f))
            except Exception as e:
                self.logger.warning(f"读取 jacoco CSV 失败 [{f}]: {e}")

        if not dfs:
            return {'jacoco_found': False, 'notes': 'jacoco CSV 读取全部失败'}

        df = pd.concat(dfs, axis=0)

        def cov(missed_col, covered_col):
            missed  = df[missed_col].sum()
            covered = df[covered_col].sum()
            total   = missed + covered
            return round(covered / total * 100, 2) if total > 0 else 0.0

        return {
            'jacoco_found':          True,
            'instruction_coverage':  cov('INSTRUCTION_MISSED', 'INSTRUCTION_COVERED'),
            'branch_coverage':       cov('BRANCH_MISSED',      'BRANCH_COVERED'),
            'line_coverage':         cov('LINE_MISSED',         'LINE_COVERED'),
            'complexity_coverage':   cov('COMPLEXITY_MISSED',   'COMPLEXITY_COVERED'),
            'method_coverage':       cov('METHOD_MISSED',       'METHOD_COVERED'),
        }


    # ── CSV 写入 ──────────────────────────────────────────────────────────────────
    def write_result(self, csv_path: Path, row: dict):
        with self.csv_lock:
            file_exists = csv_path.exists()
            with open(csv_path, 'a', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=self.CSV_FIELDS, extrasaction='ignore')
                if not file_exists:
                    writer.writeheader()
                writer.writerow(row)


    # ── 核心处理逻辑 ──────────────────────────────────────────────────────────────
    def process_project(self, project_name: str, root_dir: Path, log_dir: Path, csv_path: Path):
        project_path = root_dir / project_name
        safe_name    = project_name.replace('/', '__')
        mvn_log_file = log_dir / safe_name / 'mvn.log'
        git_log_file = log_dir / safe_name / 'git.log'

        self.logger.info(f"[{project_name}] 开始处理")

        result = {f: '' for f in self.CSV_FIELDS}
        result['project']      = project_name
        result['test_success'] = False
        result['jacoco_found'] = False
        result['notes']        = ''

        if not project_path.exists():
            result['notes'] = f'项目路径不存在: {project_path}'
            self.logger.error(f"[{project_name}] 路径不存在")
            self.write_result(csv_path, result)
            return

        # Step 1: git reset
        self.logger.info(f"[{project_name}] git reset --hard && git clean -fd")
        if not self.git_reset(project_path, git_log_file):
            result['notes'] = 'git reset 失败'
            self.logger.error(f"[{project_name}] git reset 失败")
            self.write_result(csv_path, result)
            return

        # Step 2: 获取 target 目录
        target_path = self.get_target_directory(project_path)

        # Step 3: mvn test with jacoco agent
        self.logger.info(f"[{project_name}] 运行 mvn test")
        test_ok = self.run_maven_test(project_path, target_path, mvn_log_file)
        result['test_success'] = test_ok

        if not test_ok:
            result['notes'] = 'mvn test 失败'
            self.logger.warning(f"[{project_name}] mvn test 失败")
            self.write_result(csv_path, result)
            return

        # Step 4: 收集 JaCoCo 覆盖率
        self.logger.info(f"[{project_name}] 收集 JaCoCo 覆盖率")
        cov = self.collect_jacoco_coverage(project_path)
        result.update(cov)

        if not cov.get('jacoco_found'):
            self.logger.warning(f"[{project_name}] {cov.get('notes', 'jacoco 收集失败')}")
        else:
            self.logger.info(
                f"[{project_name}] line={cov['line_coverage']}% "
                f"branch={cov['branch_coverage']}% "
                f"instruction={cov['instruction_coverage']}%"
            )

        self.write_result(csv_path, result)
        self.logger.info(f"[{project_name}] 完成")


# ── 入口 ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='批量运行 Maven 项目测试并收集 JaCoCo 覆盖率')
    parser.add_argument('--projects', required=True, help='项目列表文件，每行一个项目名')
    parser.add_argument('--root',     required=True, help='所有项目的根目录')
    parser.add_argument('--output',   required=True, help='输出目录（CSV + 日志）')
    parser.add_argument('--parallel', type=int, default=4, help='并行度（默认4）')
    args = parser.parse_args()

    root_dir   = Path(args.root).expanduser().resolve()
    output_dir = Path(args.output).expanduser().resolve()
    log_dir    = output_dir / 'logs'
    csv_path   = output_dir / f'results_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'

    output_dir.mkdir(parents=True, exist_ok=True)
    logger = get_logger(log_dir / 'runner.log', 'MavenTester')

    with open(args.projects, encoding='utf-8') as f:
        projects = [line.strip() for line in f if line.strip() and not line.startswith('#')]

    logger.info(f"共 {len(projects)} 个项目，并行度 {args.parallel}")
    logger.info(f"结果将写入: {csv_path}")

    tester = MavenTester(log_dir)
    with ThreadPoolExecutor(max_workers=args.parallel) as executor:
        futures = {
            executor.submit(tester.process_project, p, root_dir, log_dir, csv_path): p
            for p in projects
        }
        for future in as_completed(futures):
            p = futures[future]
            try:
                future.result()
            except Exception as e:
                logger.error(f"[{p}] 未捕获异常: {e}", exc_info=True)

    logger.info("全部完成")


if __name__ == '__main__':
    main()