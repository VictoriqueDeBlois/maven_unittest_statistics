import csv
import subprocess
from pathlib import Path
from collections import OrderedDict


def get_unique_projects(csv_path: str) -> list:
    """从CSV文件中提取唯一的project_name列表，保持原有顺序"""
    projects = OrderedDict()
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            project_name = row["project_name"].strip()
            if project_name and project_name not in projects:
                projects[project_name] = None
    return list(projects.keys())


def get_git_commit_time(repo_path: Path) -> dict:
    """获取git仓库的最新commit时间和hash"""
    if not (repo_path / ".git").exists():
        return {
            "commit_hash": "",
            "commit_time_iso": "",
            "commit_time_readable": "",
            "error": "not a git repo",
        }

    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H|%ci|%cI"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        parts = result.stdout.strip().split("|")
        if len(parts) >= 3:
            return {
                "commit_hash": parts[0],
                "commit_time_readable": parts[1],
                "commit_time_iso": parts[2],
                "error": "",
            }
        else:
            return {
                "commit_hash": "",
                "commit_time_iso": "",
                "commit_time_readable": "",
                "error": f"unexpected format: {result.stdout.strip()}",
            }
    except subprocess.CalledProcessError as e:
        return {
            "commit_hash": "",
            "commit_time_iso": "",
            "commit_time_readable": "",
            "error": e.stderr.strip() if e.stderr else str(e),
        }
    except Exception as e:
        return {
            "commit_hash": "",
            "commit_time_iso": "",
            "commit_time_readable": "",
            "error": str(e),
        }


def main(
    csv_path: str = "integration_benchmark_v7_n5.csv",
    repos_root: str = "/data/xuhaoran/github",
    output_csv: str = "repo_commit_times.csv",
):
    projects = get_unique_projects(csv_path)
    print(f"从 {csv_path} 中提取到 {len(projects)} 个唯一项目")

    results = []
    for idx, project_name in enumerate(projects, 1):
        repo_path = Path(repos_root) / project_name
        print(f"[{idx}/{len(projects)}] 正在处理: {project_name} ...", end=" ")

        commit_info = get_git_commit_time(repo_path)

        results.append(
            {
                "project_name": project_name,
                "repo_path": str(repo_path),
                "commit_hash": commit_info["commit_hash"],
                "commit_time_iso": commit_info["commit_time_iso"],
                "commit_time_readable": commit_info["commit_time_readable"],
                "error": commit_info["error"],
            }
        )

        if commit_info["error"]:
            print(f"失败: {commit_info['error']}")
        else:
            print(f"OK ({commit_info['commit_time_iso']})")

    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "project_name",
                "repo_path",
                "commit_hash",
                "commit_time_iso",
                "commit_time_readable",
                "error",
            ],
        )
        writer.writeheader()
        writer.writerows(results)

    print(f"\n结果已保存到: {output_csv}")


if __name__ == "__main__":
    main()
