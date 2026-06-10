# Codex 实现计划：批量统计 Maven 项目规模、类型与编译时间估计

> 当前主流程已实现为 `run_balanced_benchmark_pipeline.py`。
> 项目统计 CSV 默认写到 `data/intermediate/projects_stats_refined.csv`，
> 项目分析日志默认写到 `logs/maven_project_run_refined.log`。
> 本文保留为实现背景和字段设计参考。

## 1. 目标

实现一个 Python 脚本，用于批量分析本地已经 clone 下来的 Maven Java 项目。

项目目录结构固定为：

```text
projects_root/
  owner/
    repo/
      pom.xml
      src/
      README.md
      ...
```

脚本需要对每个项目输出以下信息：

1. 项目基本信息。
2. Maven 结构信息。
3. Java 代码规模统计。
4. 基于规模的 `mvn compile` 编译时间估计。
5. 使用 OpenAI-compatible API 进行项目类型分类。
6. 输出一个总 CSV。
7. 输出运行日志。

不要实际运行 Maven，不要执行 `mvn compile`。

---

## 2. 命令行接口

实现主入口：

```bash
python analyze_maven_projects.py \
  --projects-root /path/to/projects_root \
  --output-csv data/intermediate/projects_stats_refined.csv \
  --log-file /path/to/output/run.log
```

可选参数：

```bash
--max-readme-chars 4000
--llm-batch-size 1
--skip-llm
```

说明：

- `--projects-root`：包含 `owner/repo` 项目的根目录。
- `--output-csv`：输出 CSV 路径。
- `--log-file`：运行日志路径。
- `--max-readme-chars`：读取 README 的最大字符数，默认 4000。
- `--llm-batch-size`：第一版可以固定为 1，不需要复杂批处理。
- `--skip-llm`：跳过大模型分类，类型字段填“未知”。

不要实现 agent 相关参数。

不要实现外部 LOC 工具相关参数。

---

## 3. `.env` 配置

使用 `.env` 配置 OpenAI-compatible API。

读取以下环境变量：

```env
OPENAI_API_KEY=your_api_key_here
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4.1-mini

OPENAI_TIMEOUT_SECONDS=60
OPENAI_MAX_RETRIES=3
OPENAI_TEMPERATURE=0.2
```

要求：

- 使用 `python-dotenv` 加载 `.env`。
- 使用 OpenAI Python SDK。
- `OPENAI_BASE_URL` 必须可配置，用于支持 OpenAI-compatible API。
- 如果 `--skip-llm` 没有开启，但缺少 `OPENAI_API_KEY` 或 `OPENAI_MODEL`，脚本应报错退出。
- 如果单个项目 LLM 调用失败，不要中断全局任务，记录日志并将该项目分类设为“未知”。

---

## 4. 依赖建议

`requirements.txt`：

```text
openai
python-dotenv
tqdm
```

只使用 Python 标准库解析文件、统计 LOC/SLOC、写 CSV 和日志。

不要依赖 `cloc`、`scc`、`tokei` 等外部工具。

不要依赖 Maven。

---

## 5. 输出 CSV 字段

CSV 表头固定为：

```csv
owner,
repo,
project_path,
has_pom,
is_maven_project,
is_multi_module,
module_count,
pom_files_count,
packaging,
group_id,
artifact_id,
version,
repo_size_bytes,
total_files,
total_dirs,
java_files_count,
main_java_files,
test_java_files,
other_java_files,
main_loc,
test_loc,
other_loc,
total_loc,
main_sloc,
test_sloc,
other_sloc,
total_sloc,
dependency_count,
plugin_count,
profile_count,
has_readme,
readme_name,
top_packages,
top_level_dirs,
detected_signals,
type_label_zh,
llm_confidence,
llm_reason_zh,
llm_alternative_labels_zh,
estimated_compile_time_seconds,
compile_size_level,
compile_estimation_reason,
scan_status,
error_message
```

字段说明：

- `type_label_zh`：大模型输出的中文项目类型短语，例如“中间件”“游戏”“Web 后端”“测试框架”。
- `llm_confidence`：0 到 1。
- `llm_reason_zh`：一句中文理由。
- `llm_alternative_labels_zh`：备选标签，用分号连接。
- `estimated_compile_time_seconds`：估计的 `mvn compile` 时间。
- `compile_size_level`：`tiny`、`small`、`medium`、`large`、`huge`、`unknown`。
- `compile_estimation_reason`：一句估计理由。
- `scan_status`：`ok` 或 `failed`。
- `error_message`：失败时记录错误信息。

---

## 6. 项目发现逻辑

遍历：

```text
projects_root/*/*
```

每个二级目录视为一个项目：

```text
projects_root/owner/repo
```

规则：

- `owner` 是一级目录名。
- `repo` 是二级目录名。
- 如果项目根目录下存在 `pom.xml`，则：
  - `has_pom = true`
  - `is_maven_project = true`
- 如果没有根 `pom.xml`，则：
  - `has_pom = false`
  - `is_maven_project = false`
  - 仍然统计基本文件规模和 Java LOC/SLOC。
  - Maven 字段填空或 0。
  - LLM 仍然可以基于 README、目录结构和代码包名尝试分类。

---

## 7. 排除目录

扫描文件时排除以下目录：

```text
.git
target
build
out
.idea
.vscode
.gradle
node_modules
dist
.cache
```

这些目录不参与：

- 文件数统计
- 仓库大小统计
- Java 文件统计
- LOC/SLOC 统计
- top package 提取

---

## 8. LOC/SLOC 统计

实现内置 Java LOC/SLOC 统计器。

### 8.1 LOC 定义

`LOC` 是 `.java` 文件的物理行数，包括：

- 空行
- 注释行
- 代码行

### 8.2 SLOC 定义

`SLOC` 是去掉以下内容后的有效代码行数：

- 空行
- 纯 `//` 行注释
- 纯 `/* ... */` 块注释
- Javadoc 注释
- 只包含注释和空白的行

如果一行同时包含代码和注释，仍然算 1 行 SLOC。

例如：

```java
int x = 1; // comment
```

算 1 行 SLOC。

```java
// int x = 1;
```

算 0 行 SLOC。

```java
String url = "http://example.com";
```

算 1 行 SLOC，不要把字符串里的 `//` 当成注释。

```java
String s = "/* not comment */";
```

算 1 行 SLOC，不要把字符串里的 `/* */` 当成注释。

### 8.3 SLOC 扫描状态

实现字符级扫描，不需要 Java AST。

至少支持以下状态：

```text
NORMAL
LINE_COMMENT
BLOCK_COMMENT
STRING_LITERAL
CHAR_LITERAL
TEXT_BLOCK
```

要求：

- 能正确处理 `//` 行注释。
- 能正确处理 `/* ... */` 块注释。
- 能正确处理字符串中的 `//` 和 `/* */`。
- 能基本处理 Java text block `""" ... """`。
- 不要求和专业工具 100% 一致，但必须稳定、可解释。

---

## 9. Java 文件分类

根据路径分类 Java 文件：

```text
src/main/java/**/*.java  -> main
src/test/java/**/*.java  -> test
其他 .java 文件          -> other
```

统计字段：

```text
main_java_files
test_java_files
other_java_files

main_loc
test_loc
other_loc
total_loc

main_sloc
test_sloc
other_sloc
total_sloc
```

编译时间估计只主要使用 `main_*` 字段，因为目标是 `mvn compile`，不包含测试编译和测试运行。

---

## 10. Maven POM 解析

使用 Python 标准库 `xml.etree.ElementTree` 解析 `pom.xml`。

需要处理 XML namespace。

### 10.1 根 POM 字段

从根 `pom.xml` 提取：

```text
group_id
artifact_id
version
packaging
name
description
modules
dependencies
plugins
profiles
properties
```

注意：

- 如果根 POM 中缺少 `groupId` 或 `version`，尝试从 `<parent>` 中读取。
- `packaging` 缺失时默认为 `jar`。
- `modules` 来自 `<modules><module>...</module></modules>`。
- `is_multi_module = module_count > 0`。

### 10.2 所有 POM 统计

在项目中查找所有 `pom.xml`，排除已排除目录。

统计：

```text
pom_files_count
dependency_count
plugin_count
profile_count
```

这里的依赖、插件、profile 可以对所有 POM 汇总。

依赖去重规则：

```text
groupId:artifactId
```

插件去重规则：

```text
groupId:artifactId
```

如果 groupId 缺失，则使用：

```text
artifactId
```

---

## 11. README 提取

查找项目根目录下的 README 文件，优先级：

```text
README.md
README.MD
README.rst
README.txt
README
readme.md
```

提取字段：

```text
has_readme
readme_name
readme_excerpt
```

`readme_excerpt` 只用于 LLM 输入，不写入 CSV。

默认最多读取 `--max-readme-chars` 个字符。

读取时：

- 使用 `utf-8`。
- 如果失败，尝试 `latin-1`。
- 如果仍失败，记录日志，`has_readme = true`，但 `readme_excerpt = ""`。

---

## 12. top-level dirs 和 top packages

### 12.1 top-level dirs

统计项目根目录下的一级目录名，排除：

```text
.git
target
build
out
.idea
.vscode
.gradle
node_modules
dist
.cache
```

CSV 中用分号连接：

```text
src;docs;examples;modules
```

### 12.2 top packages

从 `src/main/java/**/*.java` 中提取 package 声明。

例如：

```java
package org.apache.dubbo.rpc;
```

可以提取前 2 到 3 段：

```text
org.apache.dubbo
```

统计出现频次，取前 10 个。

CSV 中用分号连接：

```text
org.apache.dubbo;com.example.foo
```

---

## 13. detected signals

根据依赖、插件、目录名、README 和包名生成启发式信号。

输出到 CSV 的 `detected_signals` 字段，用分号连接。

建议实现以下 signals：

```text
spring
spring_boot
servlet
maven_plugin
junit
mockito
testng
netty
grpc
jdbc
mybatis
hibernate
database
redis
kafka
rabbitmq
rocketmq
elasticsearch
lucene
hadoop
spark
flink
android
game
cli
annotation_processor
code_generation
static_analysis
logging
security
web
rpc
```

检测方法可以简单基于关键词：

- 依赖 artifactId / groupId
- plugin artifactId
- README excerpt
- top-level dirs
- repo 名称

示例：

```text
spring-boot-starter-web -> spring_boot, web
netty-all -> netty, network
grpc-core -> grpc, rpc
maven-plugin-plugin -> maven_plugin
junit -> junit
mockito -> mockito
mybatis -> mybatis, database
hibernate-core -> hibernate, database
kafka-clients -> kafka
```

---

## 14. 项目摘要构造

为 LLM 构造 JSON 项目摘要，但不直接把完整项目代码传给模型。

摘要字段：

```json
{
  "owner": "...",
  "repo": "...",
  "pom_name": "...",
  "pom_description": "...",
  "readme_excerpt": "...",
  "packaging": "...",
  "group_id": "...",
  "artifact_id": "...",
  "module_count": 0,
  "pom_files_count": 0,
  "main_java_files": 0,
  "main_sloc": 0,
  "test_java_files": 0,
  "test_sloc": 0,
  "dependencies": ["..."],
  "plugins": ["..."],
  "top_level_dirs": ["..."],
  "top_packages": ["..."],
  "detected_signals": ["..."]
}
```

控制长度：

- dependencies 最多 80 个。
- plugins 最多 50 个。
- top_level_dirs 最多 50 个。
- top_packages 最多 10 个。
- readme_excerpt 最多 `--max-readme-chars`。

---

## 15. 大模型分类

### 15.1 分类目标

大模型根据项目摘要，输出一个中文短语标签。

标签不要固定死，允许模型有自由度。

示例标签：

```text
中间件
游戏
Web 后端
数据库
数据库工具
测试框架
构建插件
Maven 插件
RPC 框架
消息队列
日志框架
搜索引擎
静态分析工具
代码生成工具
命令行工具
通用工具库
示例项目
学术原型
性能基准
```

### 15.2 System Prompt

代码中固定使用如下 system prompt：

```text
你是一个软件项目分类助手。你的任务是根据 Maven Java 项目的结构化摘要，判断该项目最合适的软件类型。

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
```

### 15.3 User Prompt 模板

代码中使用如下 user prompt：

```text
请根据下面的 Maven Java 项目摘要，判断该项目的类型。

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
```

### 15.4 LLM 返回校验

实现校验函数。

要求：

1. 必须能解析为 JSON。
2. 必须包含：
   - `type_label_zh`
   - `confidence`
   - `reason_zh`
   - `alternative_labels_zh`
3. 清理 `type_label_zh`：
   - 去掉换行。
   - 去掉首尾空格。
   - 去掉句号、冒号、引号。
4. 如果 `type_label_zh` 为空，设为“未知”。
5. 如果 `type_label_zh` 长度明显过长，例如超过 20 个字符，则重试一次。
6. 如果 `confidence` 不在 0 到 1，设为 0。
7. 如果 `alternative_labels_zh` 不是列表，设为空列表。
8. 如果调用失败或解析失败：
   - 重试最多 `OPENAI_MAX_RETRIES` 次。
   - 仍失败则：
     ```text
     type_label_zh = "未知"
     llm_confidence = 0
     llm_reason_zh = "大模型分类失败"
     llm_alternative_labels_zh = ""
     ```

---

## 16. 编译时间估计

目标是估计：

```bash
mvn compile
```

不要实际执行 Maven。

估计只基于静态规模和复杂度信号。

### 16.1 输入特征

主要使用：

```text
main_sloc
main_java_files
module_count
dependency_count
plugin_count
pom_files_count
detected_signals
```

测试代码不作为主要估计依据，因为 `mvn compile` 不编译测试代码。

### 16.2 基础等级

根据 `main_sloc` 和 `module_count` 先给等级：

```text
tiny:
  main_sloc < 2,000 且 module_count <= 1

small:
  main_sloc < 10,000 且 module_count <= 3

medium:
  main_sloc < 50,000 且 module_count <= 10

large:
  main_sloc < 200,000 且 module_count <= 50

huge:
  main_sloc >= 200,000 或 module_count > 50
```

如果没有 Java 主代码：

```text
compile_size_level = unknown
estimated_compile_time_seconds = 0
compile_estimation_reason = "未发现 src/main/java 主代码"
```

### 16.3 秒数估计规则

先计算基础时间：

```text
base_seconds = 10
```

加入代码规模：

```text
sloc_seconds = main_sloc / 500
```

加入文件数量：

```text
file_seconds = main_java_files * 0.03
```

加入模块数量：

```text
module_seconds = module_count * 8
```

加入依赖数量：

```text
dependency_seconds = dependency_count * 0.4
```

加入插件数量：

```text
plugin_seconds = plugin_count * 0.8
```

初始估计：

```text
estimated = base_seconds
          + sloc_seconds
          + file_seconds
          + module_seconds
          + dependency_seconds
          + plugin_seconds
```

### 16.4 复杂度加权

如果存在以下 signals，乘以额外系数：

```text
annotation_processor: estimated *= 1.25
code_generation: estimated *= 1.30
scala/kotlin 相关信号: estimated *= 1.40
spring_boot: estimated *= 1.10
grpc: estimated *= 1.10
protobuf/codegen 相关: estimated *= 1.25
```

第一版 signals 里如果没有 scala/kotlin/protobuf，可以通过 dependency/plugin 名称简单检测：

```text
scala-maven-plugin
kotlin-maven-plugin
protobuf-maven-plugin
maven-processor-plugin
annotation
apt
codegen
generate
```

### 16.5 限制范围

估计值做上下限控制：

```text
如果 main_sloc > 0，最小 10 秒。
最大 7200 秒。
```

最后四舍五入为整数。

### 16.6 等级和秒数对应修正

根据最终秒数修正 `compile_size_level`：

```text
tiny:   estimated < 30
small:  30 <= estimated < 120
medium: 120 <= estimated < 300
large:  300 <= estimated < 900
huge:   estimated >= 900
```

### 16.7 估计理由

生成一句中文理由：

```text
主代码约 35000 SLOC，8 个模块，120 个依赖，存在 annotation processor，因此估计为 medium。
```

---

## 17. 运行日志

使用 Python `logging`。

日志同时输出到：

1. 控制台。
2. `--log-file` 指定文件。

日志内容至少包括：

```text
开始扫描 projects_root
发现项目数量
每个项目开始处理
每个项目处理完成
POM 解析失败
README 读取失败
LLM 调用失败
LLM JSON 解析失败
CSV 写入完成
总耗时
```

示例：

```text
[INFO] Found 1234 candidate projects.
[INFO] Processing apache/dubbo
[INFO] Finished apache/dubbo: status=ok, label=RPC 框架, estimated_compile_time=1530
[WARNING] Failed to parse pom for foo/bar: ...
[ERROR] LLM classification failed for abc/xyz: ...
```

---

## 18. 错误处理

脚本必须保证单个项目失败不会影响其他项目。

每个项目处理时使用 try/except 包住整体逻辑。

如果某个项目失败：

```text
scan_status = failed
error_message = 简短错误信息
```

仍然写入 CSV 一行。

如果只是部分失败，例如 POM 解析失败但 LOC 统计成功：

```text
scan_status = ok
error_message = "pom parse failed: ..."
```

即：只有项目整体无法扫描时才标记 `failed`。

---

## 19. 建议代码结构

可以单文件实现，也可以模块化。建议模块化：

```text
analyze_maven_projects.py
maven_project_analyzer/
  __init__.py
  discovery.py
  file_scanner.py
  java_loc_counter.py
  pom_parser.py
  readme_extractor.py
  signal_detector.py
  project_summary.py
  llm_classifier.py
  compile_estimator.py
  csv_writer.py
  logging_utils.py
```

如果为了 Codex 一次实现方便，也可以先写成一个脚本，但内部函数要清晰拆分。

核心函数建议：

```python
discover_projects(projects_root) -> list[ProjectRef]

scan_files(project_path) -> FileStats

count_java_loc(java_file_path) -> JavaLocStats

parse_root_pom(project_path) -> RootPomInfo

parse_all_poms(project_path) -> PomAggregateStats

extract_readme(project_path, max_chars) -> ReadmeInfo

extract_top_packages(project_path) -> list[str]

detect_signals(...) -> list[str]

build_project_summary(...) -> dict

classify_project_with_llm(summary) -> LlmClassification

estimate_compile_time(stats, signals) -> CompileEstimate

write_csv(rows, output_csv)
```

---

## 20. 最终处理流程

每个项目的流程：

```text
1. 从 projects_root/owner/repo 发现项目。

2. 初始化结果行，填入 owner、repo、project_path。

3. 扫描文件：
   - 统计 total_files、total_dirs、repo_size_bytes。
   - 查找所有 .java 文件。
   - 按 main/test/other 分类。
   - 对每个 Java 文件计算 LOC/SLOC。

4. 检查根 pom.xml：
   - 判断 has_pom、is_maven_project。
   - 解析 group_id、artifact_id、version、packaging、modules。
   - 判断 is_multi_module、module_count。

5. 解析所有 pom.xml：
   - 统计 pom_files_count。
   - 汇总 dependency_count、plugin_count、profile_count。
   - 保留 dependency/plugin 列表给 LLM 和 signal detector。

6. 提取 README：
   - has_readme。
   - readme_name。
   - readme_excerpt。

7. 提取 top-level dirs 和 top packages。

8. 生成 detected_signals。

9. 构造 project_summary_json。

10. 如果未开启 --skip-llm：
    - 调用 OpenAI-compatible API。
    - 获得 type_label_zh、confidence、reason_zh、alternative_labels_zh。
    - 校验和清洗结果。
    如果开启 --skip-llm：
    - type_label_zh = "未知"
    - llm_confidence = 0
    - llm_reason_zh = "跳过大模型分类"
    - llm_alternative_labels_zh = ""

11. 根据 main_sloc、main_java_files、module_count、dependency_count、plugin_count、signals 估计 mvn compile 时间。

12. 写入 CSV 行。

13. 记录日志。
```

---

## 21. 验收标准

实现完成后，应该满足：

1. 可以运行：

```bash
python analyze_maven_projects.py \
  --projects-root ./projects_root \
  --output-csv data/intermediate/projects_stats_refined.csv \
  --log-file ./output/run.log
```

2. 不执行任何 Maven 命令。

3. 不联网搜索 GitHub。

4. 不使用 agent。

5. 不调用 `cloc`、`scc`、`tokei` 等外部 LOC 工具。

6. LOC/SLOC 由内置 Java-aware counter 统计。

7. 每个 `projects_root/owner/repo` 至少输出一行 CSV。

8. 大模型分类结果为中文短语，例如：

```text
中间件
Web 后端
游戏
测试框架
通用工具库
```

9. 即使某个项目 POM 解析失败、README 读取失败、LLM 调用失败，也不会中断全局任务。

10. 最终输出只有：

```text
data/intermediate/projects_stats.csv
run.log
```

不需要 JSONL，不需要缓存文件。
