# Maven 测试用例指标统计工具 — 设计文档

> 当前主流程入口是 `run_balanced_benchmark_pipeline.py`。
> 主流程使用 `maven_test_metrics_jar.py` 调用 Java jar 生成指标；
> 旧 Python AST 版 `maven_test_metrics.py` 已归档到 `archive/legacy_code_20260610/`。
> 当前中间产物统一放在 `data/intermediate/`，日志统一放在 `logs/`，最终结果放在 `outputs/`。

## 一、工具概述

本工具用于统计 Maven 项目中每个 `@Test` 方法的以下指标，输出为 CSV 文件：

| 字段 | 含义 |
|------|------|
| `project_name` | 项目名称 |
| `test_full_name` | 测试方法全名，格式 `com.example.MyTest#testMethod` |
| `setup_length` | 展开后所有测试基础代码的非断言有效行数之和 |
| `assertion_count` | 展开后的断言调用总次数 |
| `mock_verify_count` | 展开后的 Mockito verify 调用次数 |
| `uses_mock` | 是否使用了 Mock |
| `called_project_methods` | 展开后调用的生产代码方法列表（JSON 数组，`类FQN.方法名`） |

工具有两个实现版本：

- **Python 版**（`maven_test_metrics.py`）：基于 javalang 解析 AST，手工推断类型
- **Java 版**（`maven-test-metrics-java/`）：基于 JavaParser + SymbolSolver，直接做类型解析

---

## 二、核心概念：测试代码展开

测试方法通常依赖测试基类（如 `CardTestPlayerBase`、`CardTestPlayerAPIImpl`）提供的辅助方法（`addCard`、`castSpell`、`execute` 等）。这些方法本身也包含有效代码、断言和生产代码调用，必须递归展开才能得到准确指标。

**展开规则：**
- 只展开**无 qualifier 的方法调用**（即继承链上的测试基础方法）
- 只展开声明在 `src/test/java` 中的方法
- 有 qualifier 的调用（如 `game.getPlayer()`）是被测生产代码，**不展开，只收集**
- 最大递归深度 5 层，防止循环展开

```
testDestroyCreatureLifeLoss()          ← 顶层 @Test 方法
  ├── addCard(...)                     ← 展开（测试基础方法）
  │     ├── CardRepository.findCard()  ← 收集（生产代码调用）
  │     └── game.getPlayers()          ← 收集（生产代码调用）
  ├── execute()                        ← 展开
  │     ├── game.start()               ← 收集
  │     └── ...
  └── assertLife()                     ← 展开（断言方法，计入 assertion_count）
```

---

## 三、Python 版实现（maven_test_metrics.py）

### 3.1 项目结构发现

扫描项目根目录，找到所有 `src/main/java`（生产代码）和 `src/test/java`（测试代码）目录。

```python
for src_dir in PROJECT_ROOT.rglob("src/main/java"): ...
for test_dir in PROJECT_ROOT.rglob("src/test/java"): ...
```

### 3.2 类索引与生产类集合

遍历所有源文件，解析 package 声明和顶层类型声明，建立 `FQN → 文件路径` 的索引。

```python
class_index: Dict[str, Path]          # mage.game.Game → /path/Game.java
source_class_set: Set[str]            # 仅 src/main/java 中的类 FQN
```

**关键细节：**
- 只索引顶层 class/interface/enum，跳过内部类，避免同名内部类（如两个文件里都有 `EventType` 内部枚举）造成 FQN 冲突
- 当检测到重复 FQN 时输出 warning 日志

### 3.3 继承链方法表（all_methods）

从目标测试类出发，递归解析父类（通过 import 解析 FQN，再从 class_index 找源文件），收集整个继承链上的所有方法：

```python
all_methods: Dict[str, MethodNode]    # 方法名 → AST 节点（子类优先）
all_methods_files: Dict[str, Path]    # 方法名 → 所在文件
inheritance_field_map: Dict[str, str] # 字段名 → 类型 FQN（含父类字段）
```

### 3.4 生产代码调用收集（_collect_called_project_methods）

对展开后的所有 `MethodInvocation` 节点，按以下优先级解析 qualifier 的类型：

| 优先级 | qualifier 形式 | 解析方式 |
|--------|---------------|----------|
| 1 | 类名（静态调用） | 当前文件 import_map |
| 2 | 类名（在父类文件导入） | 继承链合并 combined_import_map |
| 3 | 局部变量名 | 方法体内 `LocalVariableDeclaration` + 形参 `FormalParameter` |
| 4 | 字段名 | 继承链全量 inheritance_field_map |
| 5 | 链式（含 `.`） | 取根部分查 import_map / combined_import_map |

解析出的类型 FQN 若在 `source_class_set` 中，则认为是生产代码调用并收集。

**局限性：**  
无法处理方法调用返回值上的链式调用（如 `game.getPlayer(id).getLife()`），因为 javalang 不做类型推断，`getLife()` 的 qualifier 为 `None`，无法区分是继承方法还是生产代码调用。

### 3.5 有效代码行统计

对每个展开的方法，统计从方法体第一行到最后一行中：
- 非空行
- 非注释行（`//`、`/*`、`*` 开头）
- 非纯大括号行（`{`、`}`）
- 非断言行（包含断言方法调用的行）

断言方法判定：方法名以 `assert` 开头，或为 `fail`，或在 JUnit 标准断言集合中。

---

## 四、Java 版实现（maven-test-metrics-java/）

### 4.1 核心优势

使用 **JavaParser + SymbolSolver** 做完整的跨文件类型推断，`call.resolve()` 可以直接得到任意方法调用的声明类 FQN，无需手工维护 import_map / field_map / local_var_map：

```java
// 任意形式的调用（局部变量、字段、链式、形参）都能正确解析
ResolvedMethodDeclaration resolved = call.resolve();
String declaringClass = resolved.declaringType().getQualifiedName();
// → "mage.game.Game"  直接准确，不需要任何手工推断
```

这意味着：
- `obj.method()` ✓（局部变量）
- `this.field.method()` ✓（字段）
- `CardRepository.instance.findCard()` ✓（链式静态字段）
- `getPlayer(id).getLife()` ✓（方法返回值链式调用，Python 版无法处理）
- 形参上的调用 ✓

### 4.2 SymbolSolver 配置关键

`JavaParserTypeSolver` 必须与主解析器共享同一个 `ParserConfiguration`，否则它在内部解析父类文件时不会做类型推断，导致继承链断裂、resolution 全部失败：

```java
// 正确顺序：
CombinedTypeSolver typeSolver = new CombinedTypeSolver();
typeSolver.add(new ReflectionTypeSolver(false));         // ① JDK 类型

JavaSymbolSolver symbolSolver = new JavaSymbolSolver(typeSolver);
ParserConfiguration config = new ParserConfiguration()
    .setSymbolResolver(symbolSolver);                    // ② 创建带 resolver 的 config

// ③ 关键：把同一个 config 传给 JavaParserTypeSolver
for (Path dir : sourceDirs) typeSolver.add(new JavaParserTypeSolver(dir, config));
for (Path dir : testDirs)   typeSolver.add(new JavaParserTypeSolver(dir, config));

JavaParser parser = new JavaParser(config);              // ④ 主 parser 用同一 config
```

### 4.3 构建流程

```
项目根目录
  ↓ findDirs("src/main/java") / findDirs("src/test/java")
源码目录列表
  ↓ buildClassSet()（正则提取 package + 文件名）
productionClassSet / testClassSet（FQN 集合）
  ↓ 配置 SymbolSolver
JavaParser（支持跨文件类型解析）
  ↓ findTestFiles() → 遍历每个 @Test 方法
TestMethodAnalyzer.analyze()
  ├── expandMethodCalls()   递归展开测试基础方法
  ├── countEffectiveLines() 统计非断言有效行
  ├── collectProductionCalls() 用 call.resolve() 收集生产调用
  └── TestMetrics
```

### 4.4 局限性

- `productionClassSet` 基于文件名推断 FQN（一文件一顶层类的 Java 惯例），内部类不被索引
- 解析器外部依赖（jar 包中的类型）resolution 会失败并被跳过，不影响项目内代码的收集
- 扩展仅限无 qualifier 调用（与 Python 版行为一致）

---

## 五、debug_analysis.py — 单方法调试脚本

### 用途

对单个指定的测试方法进行详细分析，打印人类可读的调试信息，方便与真实代码对比验证统计结果是否正确。

### 配置

脚本顶部的三个常量：

```python
PROJECT_ROOT  = Path("/path/to/project")
TARGET_FILE   = PROJECT_ROOT / "path/to/SomeTest.java"
TARGET_METHOD = "testMethodName"
```

### 输出内容（7 个阶段）

#### 阶段 1-4：项目结构与索引
输出生产包数量、类索引总数、目标方法位置、继承链方法数/字段数等基础信息。

#### 阶段 5：方法调用树（树状结构）

递归打印测试方法的完整展开树，每个节点标注：
- `[测试]` — 测试基础类方法（会递归展开）
- `[生产]` — 生产代码方法调用（不展开，标注 FQN）
- `(已展开↑)` — 该方法已在上方展开过，避免重复

示例输出：
```
testDestroyCreatureLifeLoss [测试]  L30  9行
├── addCard [测试]  L649  39行
│   ├── [生产] mage.cards.repository.CardRepository.findCard
│   ├── [生产] mage.game.Game.getPlayers (×2)
│   └── getCardList [测试]  L743  14行
├── execute [测试]  L235  38行
│   ├── [生产] mage.game.Game.start
│   └── assertAllCommandsUsed [测试]  L1594  2行
└── assertLife [测试]  L846  1行
    └── [生产] mage.game.Game.getPlayer
```

每个节点显示：行号（`L30`）和该方法的有效非断言代码行数（`9行`）。

#### 阶段 6：最终指标汇总

打印与 CSV 输出完全对应的 5 项指标：

```
setup_length          = 187
assertion_count       = 25
mock_verify_count     = 0
uses_mock             = False
called_project_methods= ["mage.game.Game.getPlayers", ...]
```

#### 阶段 7：有效代码注释文件

将每个展开方法的源代码逐行写入 `debug/<方法名>.txt`，每行标注：

| 标签 | 含义 |
|------|------|
| `[计数]` | 计入 setup_length 的有效行 |
| `[断言]` | 断言行（不计入 setup_length） |
| `[跳过]` | 空行 / 注释 / 纯大括号（不计入） |
| `[签名]` | 方法签名行（不计入） |

生产代码调用行额外追加 `← [生产] FQN` 标注。

示例：
```
[签名]    30      public void testDestroyCreatureLifeLoss(){
[计数]    31          addCard(Zone.HAND, playerA, "Unlicensed Disintegration");
[跳过]    36          // Need an artifact to trigger the damage
[计数]    37          addCard(Zone.BATTLEFIELD, playerA, "Sol Ring");
[断言]    47          assertLife(playerA, 20);
...
>>> 本方法计入行数: 9  断言调用次数: 4

======================================================================
方法: addCard  (CardTestPlayerAPIImpl.java:649)
======================================================================
[计数]   672              cardInfo = CardRepository.instance.findCard(cardName, true);  ← [生产] mage.cards.repository.CardRepository.findCard
...
>>> 本方法计入行数: 39  断言调用次数: 3

======================================================================
>>> setup_length 合计: 187  assertion_count 合计: 25
```

---

## 六、Python 版 vs Java 版对比

| 维度 | Python 版 | Java 版 |
|------|-----------|---------|
| 解析库 | javalang（纯语法解析） | JavaParser + SymbolSolver（类型推断） |
| 类型解析 | 手工推断（import/field/local_var map） | `call.resolve()` 自动解析 |
| 链式调用 | 仅支持一层（`A.b.method()`） | 任意层（`a.foo().bar().baz()`） |
| 方法返回值调用 | ❌ 无法处理 | ✅ 完全支持 |
| 外部依赖处理 | 基于 FQN 精确匹配 | resolution 失败时静默跳过 |
| 调试工具 | debug_analysis.py（详细树状输出） | 无（可添加） |
| 运行速度 | 快（无类型解析） | 较慢（跨文件类型推断） |
| 构建方式 | `uv run python maven_test_metrics.py` | `mvn package` → `java -jar` |
