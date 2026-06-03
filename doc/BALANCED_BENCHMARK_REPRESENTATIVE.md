# balanced_benchmark_representative 生成流程说明

本文档说明当前脚本如何生成 `balanced_benchmark_representative`，以及它的筛选和抽样机制。核心逻辑在 `build_balanced_benchmark.py`，`main.py` 中的 `build_balanced_benchmark()` 只是固定参数调用入口。

## 1. 当前入口和参数

`main.py` 中的调用参数是：

```bash
python build_balanced_benchmark.py \
  --projects projects_stats_refined.csv \
  --output-dir balanced_benchmark_representative \
  --exclude-projects bad_projects.txt
```

未显式传入的参数使用 `build_balanced_benchmark.py` 默认值：

- `--tests all_tests_jar.csv`
- `--commits repo_commit_times.csv`
- `--target-projects 50`
- `--tests-per-project 5`
- `--min-count 200`
- `--max-count 400`
- `--min-packages 5`
- `--min-llm-confidence 0.70`
- `--max-compile-time-seconds 1800`
- `--max-huge-projects 6`

所以当前目标是：从有效 Maven 项目中选出 50 个代表性项目，每个项目选 5 条测试，总计 250 条测试，输出到 `balanced_benchmark_representative/`。

注意：`main.py` 里当前 `if __name__ == '__main__'` 部分把 `build_balanced_benchmark()` 调用注释掉了，实际执行的是基于已有 `balanced_benchmark_representative/balanced_tests.csv` 提取测试代码。如果要重新生成 CSV，需要取消注释或直接运行上面的命令。

## 2. 输入文件

生成 CSV 阶段读取三个主要输入：

- `all_tests_jar.csv`：测试方法级指标，包括 `project_name`、`test_full_name`、`uses_mock`、`called_packages_count`、`called_methods_count`、`setup_length`、`assertion_count` 等。
- `projects_stats_refined.csv`：项目级统计和 LLM 标注，包括 `owner`、`repo`、`type_label_zh`、`compile_size_level`、`estimated_compile_time_seconds`、`main_sloc`、`main_java_files`、`llm_confidence` 等。
- `repo_commit_times.csv`：项目对应的 commit 信息，合并到最终输出。

此外，`bad_projects.txt` 是人工排除列表，当前输出记录显示排除了 1 个项目：`aws/aws-sdk-java-v2`。

## 3. 总体生成流程

脚本按下面顺序生成 benchmark：

1. 读取项目统计，并生成标准项目名 `owner/repo`。
2. 合并 commit 信息。
3. 过滤掉元数据不合格或不适合作为 Maven benchmark 的项目。
4. 根据项目中文类型标签和信号归并项目领域 `project_domain`。
5. 读取所有测试指标。
6. 删除重复测试名对应的所有行。
7. 过滤 mock 测试、复杂度不足的测试、人工排除项目中的测试。
8. 将测试和有效项目内连接，只保留有效项目里的有效测试。
9. 根据项目规模和 `called_packages_count` 计算测试难度。
10. 按领域和项目规模轮转选择 50 个代表性项目。
11. 每个代表性项目内部选复杂度最高的 5 条测试。
12. 写出总 CSV、按难度拆分的 CSV、项目汇总、summary、排除日志和图表。

## 4. 项目筛选机制

项目过滤发生在 `_valid_projects()`，满足以下条件的项目才会进入候选池：

- `scan_status == ok`
- `has_pom` 为真
- `is_maven_project` 为真
- 不是 Gradle metadata POM 项目
- `type_label_zh` 不是空、unknown、未知、无法判断、不明确、N/A、示例项目等不清晰标签
- `compile_size_level` 必须是 `tiny`、`small`、`medium`、`large`、`huge` 之一
- `estimated_compile_time_seconds` 必须存在且大于 0
- `main_java_files > 0`
- `main_sloc > 0`
- 如果有 `llm_confidence` 列，则必须 `>= 0.70`

之后还有一层项目过滤：

- `estimated_compile_time_seconds <= 1800`
- 如果命令行指定了 `--exclude-size-level`，会排除对应规模；当前没有排除任何规模。

## 5. 领域归并机制

脚本不会直接使用原始 `type_label_zh` 做均衡，而是归并到更粗的 `project_domain`。

归并优先级：

1. 先在 `type_label_zh` 和 `llm_alternative_labels_zh` 中匹配关键词。
2. 如果未命中，再在 `detected_signals` 中匹配关键词。
3. 如果仍未命中，归为 `其他明确领域`。

当前规则会归并到这些领域：

- 安全与风控
- 云服务与SDK
- 分布式与中间件
- 数据处理与计算
- 数据库与存储
- Web与业务系统
- 开发与构建工具
- 测试与质量
- 网络与协议
- 文档与媒体
- 游戏与图形
- 其他明确领域

## 6. 测试用例筛选机制

测试过滤发生在 `_prepare_candidates()`，规则如下：

1. 删除重复测试：
   - 以 `(project_name, test_full_name)` 判断重复。
   - 只要某个测试全名出现重复，脚本会删除这个重复组的所有行，而不是保留其中一行。
2. 排除 mock 测试：
   - `uses_mock` 标准化为小写字符串后，等于 `true` 的测试会被排除。
3. 排除调用包数量不足的测试：
   - `called_packages_count < 5` 的测试会被排除。
4. 排除人工黑名单项目：
   - `project_name` 出现在 `bad_projects.txt` 中的测试会被排除。
5. 只保留能匹配到有效项目的测试：
   - 测试表和有效项目表按 `project_name` 做 inner join。

脚本还会给候选测试追加两个标注列：

- `has_keyword`
- `matched_keyword`

这两个字段来自集成测试关键词匹配，只作为观察标注，不参与筛选。

## 7. 难度计算机制

每条候选测试会获得 `difficulty_score` 和 `difficulty`。

难度分数由两部分相加：

1. 项目规模分：
   - `tiny = 1`
   - `small = 2`
   - `medium = 3`
   - `large = 4`
   - `huge = 5`
2. 调用包数量分：
   - `called_packages_count >= 15`：5 分
   - `>= 10`：4 分
   - `>= 7`：3 分
   - `>= 5`：2 分
   - `< 5`：1 分，但当前 `< 5` 已被测试过滤排除

总分映射为：

- `score <= 4`：`easy`
- `score <= 6`：`medium`
- `score <= 8`：`hard`
- `score >= 9`：`expert`

因此，难度不是人工标注，也不是单独由测试方法复杂度决定，而是由“项目规模 + 测试调用外部包数量”共同决定。

## 8. 代表性项目选择机制

代表性项目选择发生在 `_select_representative_projects()`。机制如下：

1. 先按 `(project_name, project_domain, project_compile_size_level)` 分组。
2. 每个项目内部先按复杂度排序，排序字段是：
   - `difficulty_score` 降序
   - `called_packages_count` 降序
   - `called_methods_count` 降序
   - `setup_length` 降序
   - `test_full_name` 升序
3. 对每个项目取前 `tests_per_project=5` 条作为 preview，用来计算项目级排序特征。
4. 候选项目必须至少有 5 条候选测试，否则不能被选为代表性项目。
5. 项目级排序优先级是：
   - preview 中 `easy` 数量越少越优先
   - preview 中 `hard` 或 `expert` 数量越多越优先
   - preview 平均难度分越高越优先
   - 项目最大难度分越高越优先
   - 项目最大 `called_packages_count` 越高越优先
   - 候选测试数量越多越优先
   - 预计编译时间越短越优先
   - `project_name` 字典序作为最后稳定排序
6. 排序后的项目会放入 `(project_domain, project_compile_size_level)` 单元格。
7. 脚本轮转这些单元格选项目，优先选择当前已选数量少的单元格，以保持领域和规模分布更均衡。
8. 同等情况下优先选择规模更大的单元格。
9. `huge` 项目总数最多 6 个。
10. 直到选满 50 个项目，或没有可选项目为止。

这个机制的核心目标是“领域 × 项目规模”的代表性，同时偏向复杂测试更多、候选测试更多、编译时间更短的项目。

## 9. 每个项目内测试选择机制

项目选定后，`_pick_tests_for_projects()` 会对每个项目再次按下面顺序排序，并取前 5 条：

- `difficulty_score` 降序
- `called_packages_count` 降序
- `called_methods_count` 降序
- `setup_length` 降序
- `test_full_name` 升序

因此最终每个项目固定 5 条测试，且是该项目中按当前指标看最复杂、最有代表性的 5 条。

## 10. 当前输出结果

当前 `balanced_benchmark_representative/summary.txt` 显示：

- 选中测试数：250
- 选中项目数：50
- 覆盖领域数：11
- 每项目测试数：5
- 最小 `called_packages_count`：5
- 排除 mock：是
- 发现重复测试项目：94
- 删除重复测试全名：4500
- 删除重复测试行：20472
- 人工排除项目数：1
- 最大编译时间：1800 秒
- 最大 huge 项目数：6

难度分布：

- `easy`: 22
- `medium`: 87
- `hard`: 126
- `expert`: 15

领域分布：

- 数据库与存储：50
- Web与业务系统：40
- 开发与构建工具：30
- 数据处理与计算：30
- 分布式与中间件：30
- 测试与质量：20
- 文档与媒体：15
- 网络与协议：15
- 安全与风控：10
- 游戏与图形：5
- 云服务与SDK：5

项目规模分布：

- `small`: 25
- `medium`: 55
- `large`: 140
- `huge`: 30

这里的规模分布是按测试数统计。因为每个项目 5 条测试，所以可换算为：

- `small`: 5 个项目
- `medium`: 11 个项目
- `large`: 28 个项目
- `huge`: 6 个项目

## 11. 输出文件

`balanced_benchmark_representative/` 下主要产物：

- `balanced_tests.csv`：最终 250 条测试。
- `balanced_tests_easy.csv`、`balanced_tests_medium.csv`、`balanced_tests_hard.csv`、`balanced_tests_expert.csv`：按难度拆分。
- `selected_projects.csv`：50 个项目的汇总信息。
- `summary.txt`：总体统计。
- `excluded_projects.log`：重复测试删除情况、人工排除项目。
- `charts/`：难度、领域、项目规模等图表。
- `testcases/annotated/` 和 `testcases/raw_java/`：由 `extract_test_snippets.py` 基于 `balanced_tests.csv` 抽取出的测试代码。

## 12. 需要注意的实现细节

- 当前脚本是确定性的：没有随机种子，也没有随机抽样；相同输入和参数会得到相同输出。
- `DIFFICULTY_QUOTAS` 和 `_balanced_pick()` 在脚本中存在，但当前 `build_balanced_benchmark()` 路径没有使用它们。因此当前代表性版本不是按 easy/medium/hard/expert 固定比例抽样。
- `max_per_project` 参数在当前代表性项目路径中也没有实际限制作用，因为实际逻辑是先选 50 个项目，再每个项目固定取 5 条。
- 重复测试处理比较严格：发现同一项目内同一 `test_full_name` 重复时，会删除该重复测试名的所有记录。
- 集成测试关键词匹配只生成 `has_keyword` 和 `matched_keyword` 标注，不决定测试是否被选中。

## 13. 一步一步运行生成 balanced_benchmark_representative

### 13.1 推荐调用方式

不要直接依赖当前 `main.py` 生成 CSV。当前 `main.py` 的主入口里，`build_balanced_benchmark()` 是注释掉的，实际执行的是：

1. 删除 `balanced_benchmark_representative/testcases`
2. 从已有的 `balanced_benchmark_representative/balanced_tests.csv` 抽取 annotated 代码
3. 从同一个 CSV 抽取 raw Java 代码

也就是说，当前直接运行：

```bash
uv run python main.py
```

不会重新生成 `balanced_tests.csv`，只会基于已有 CSV 重新提取代码。

推荐把生成 CSV 和提取代码拆开手动调用，流程更清楚。

### 13.2 最短路径：已有中间产物时重新生成 benchmark

如果下面文件已经存在：

- `all_tests_jar.csv`
- `projects_stats_refined.csv`
- `repo_commit_times.csv`
- `bad_projects.txt`
- 本地项目源码根目录 `/data/xuhaoran/github`
- Java 分析 jar `/data/xuhaoran/idea/maven-test-metrics-java/target/maven-test-metrics-1.0-SNAPSHOT.jar`

则按下面三步跑。

第一步，生成 `balanced_tests.csv` 和统计产物：

```bash
uv run python build_balanced_benchmark.py \
  --tests all_tests_jar.csv \
  --projects projects_stats_refined.csv \
  --commits repo_commit_times.csv \
  --output-dir balanced_benchmark_representative \
  --exclude-projects bad_projects.txt
```

这一步会生成：

- `balanced_benchmark_representative/balanced_tests.csv`
- `balanced_benchmark_representative/balanced_tests_easy.csv`
- `balanced_benchmark_representative/balanced_tests_medium.csv`
- `balanced_benchmark_representative/balanced_tests_hard.csv`
- `balanced_benchmark_representative/balanced_tests_expert.csv`
- `balanced_benchmark_representative/selected_projects.csv`
- `balanced_benchmark_representative/summary.txt`
- `balanced_benchmark_representative/excluded_projects.log`
- `balanced_benchmark_representative/charts/`

第二步，提取带标注版本测试代码：

```bash
uv run python extract_test_snippets.py \
  --csv balanced_benchmark_representative/balanced_tests.csv \
  --root /data/xuhaoran/github \
  --output balanced_benchmark_representative/testcases/annotated \
  --mode all \
  --workers 20 \
  --jar /data/xuhaoran/idea/maven-test-metrics-java/target/maven-test-metrics-1.0-SNAPSHOT.jar \
  --format annotated
```

第三步，提取 raw Java 版本测试代码：

```bash
uv run python extract_test_snippets.py \
  --csv balanced_benchmark_representative/balanced_tests.csv \
  --root /data/xuhaoran/github \
  --output balanced_benchmark_representative/testcases/raw_java \
  --mode all \
  --workers 20 \
  --jar /data/xuhaoran/idea/maven-test-metrics-java/target/maven-test-metrics-1.0-SNAPSHOT.jar \
  --format raw-java
```

跑完后，完整目录就是：

```text
balanced_benchmark_representative/
  balanced_tests.csv
  balanced_tests_easy.csv
  balanced_tests_medium.csv
  balanced_tests_hard.csv
  balanced_tests_expert.csv
  selected_projects.csv
  summary.txt
  excluded_projects.log
  charts/
  testcases/
    annotated/
    raw_java/
```

### 13.3 如果要用 main.py 的封装函数

`main.py` 里已经有一个函数：

```python
def build_balanced_benchmark():
    import build_balanced_benchmark
    args = [
        '--projects', 'projects_stats_refined.csv',
        '--output-dir', 'balanced_benchmark_representative',
        '--exclude-projects', 'bad_projects.txt'
    ]
```

这个函数等价于调用：

```bash
uv run python build_balanced_benchmark.py \
  --projects projects_stats_refined.csv \
  --output-dir balanced_benchmark_representative \
  --exclude-projects bad_projects.txt
```

因为 `build_balanced_benchmark.py` 有默认参数，所以它会默认读取：

- `--tests all_tests_jar.csv`
- `--commits repo_commit_times.csv`

如果想通过 `main.py` 一键生成 CSV，需要把 `main.py` 里的这一行取消注释：

```python
# build_balanced_benchmark()
```

但更稳妥的方式还是直接运行 `build_balanced_benchmark.py`，因为命令行参数一眼能看清楚。

### 13.4 从更早阶段重建中间产物

通常不需要每次重建这些中间产物。如果缺失或源码发生了大规模更新，再按下面顺序重建。

第一步，重新生成测试指标总表 `all_tests_jar.csv`：

```bash
uv run python maven_test_metrics_jar.py \
  --projects all_repos.txt \
  --root /data/xuhaoran/github \
  --output all_tests_jar.csv \
  --workers 20 \
  --jar /data/xuhaoran/idea/maven-test-metrics-java/target/maven-test-metrics-1.0-SNAPSHOT.jar \
  --timeout 1800
```

这一步扫描所有仓库测试，产出测试方法级指标。`balanced_benchmark_representative` 的测试筛选就是从这个 CSV 开始。

第二步，重新生成项目统计 `projects_stats_refined.csv`：

```bash
uv run python analyze_maven_projects.py \
  --projects-root /data/xuhaoran/github \
  --output-csv projects_stats_refined.csv \
  --log-file maven_project_run_refined.log
```

这一步会做项目结构统计和 LLM 类型标注。如果已有旧的 `projects_stats.csv`，并且只想复用已有 LLM 标签，可以改用：

```bash
uv run python analyze_maven_projects.py \
  --projects-root /data/xuhaoran/github \
  --output-csv projects_stats_refined.csv \
  --log-file maven_project_run_refined.log \
  --reuse-llm-csv projects_stats.csv
```

第三步，重新生成 commit 信息 `repo_commit_times.csv`：

```bash
uv run python -c "import get_repo_commit_times; get_repo_commit_times.main(csv_path='all_tests_jar.csv', repos_root='/data/xuhaoran/github', output_csv='repo_commit_times.csv')"
```

第四步，维护人工排除项目：

```text
bad_projects.txt
```

每行写一个 `owner/repo`。当前内容是：

```text
aws/aws-sdk-java-v2
```

第五步，再回到 13.2，运行 `build_balanced_benchmark.py` 和两次 `extract_test_snippets.py`。

### 13.5 当前完整链路图

```text
all_repos.txt
  -> maven_test_metrics_jar.py
  -> all_tests_jar.csv

/data/xuhaoran/github
  -> analyze_maven_projects.py
  -> projects_stats_refined.csv

all_tests_jar.csv + /data/xuhaoran/github
  -> get_repo_commit_times.py
  -> repo_commit_times.csv

all_tests_jar.csv
projects_stats_refined.csv
repo_commit_times.csv
bad_projects.txt
  -> build_balanced_benchmark.py
  -> balanced_benchmark_representative/balanced_tests.csv
  -> balanced_benchmark_representative/summary.txt
  -> balanced_benchmark_representative/selected_projects.csv
  -> balanced_benchmark_representative/charts/

balanced_benchmark_representative/balanced_tests.csv
/data/xuhaoran/github
Java metrics jar
  -> extract_test_snippets.py --format annotated
  -> balanced_benchmark_representative/testcases/annotated/

balanced_benchmark_representative/balanced_tests.csv
/data/xuhaoran/github
Java metrics jar
  -> extract_test_snippets.py --format raw-java
  -> balanced_benchmark_representative/testcases/raw_java/
```
