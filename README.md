# Maven测试用例指标统计工具

## 功能说明

该工具用于统计Maven项目中的测试用例指标，支持：

1. **测试预言长度**：从测试方法开始到第一个断言之间的代码行数（排除空行和注释）
2. **断言数量**：JUnit断言方法的调用次数
3. **Mock验证次数**：Mockito `verify()` 方法的调用次数
4. **是否使用Mock**：检测是否使用了Mockito框架
5. **调用的项目内方法**：统计测试用例调用的所有项目源代码中的方法

## 特性

- ✅ 支持JUnit 4和JUnit 5
- ✅ 支持Maven多模块项目
- ✅ 自动展开测试类的private方法
- ✅ 递归处理private方法调用（避免循环递归）
- ✅ 自动识别项目包名，区分项目内外方法
- ✅ 并行处理多个项目
- ✅ 进度条显示处理进度
- ✅ 详细的错误日志记录

## 安装依赖

```bash
pip install -r requirements.txt --break-system-packages
```

或手动安装：

```bash
pip install javalang==0.13.0 tqdm==4.66.1 --break-system-packages
```

## 使用方法

### 1. 准备项目列表文件

创建一个文本文件（如 `projects.txt`），每行一个项目名称：

```
killme2008/aviatorscript
apache/commons-lang
google/guava
```

### 2. 运行分析

```bash
python3 maven_test_metrics.py \
    --projects projects.txt \
    --root /path/to/repos \
    --output test_metrics.csv \
    --workers 4
```

### 参数说明

- `--projects`: 项目列表文件路径（必需）
- `--root`: 所有项目的根目录（必需）
  - 例如，如果根目录是 `/home/user/repos`，项目名是 `killme2008/aviatorscript`
  - 则实际项目路径为 `/home/user/repos/killme2008/aviatorscript`
- `--output`: 输出CSV文件路径（默认：`test_metrics.csv`）
- `--workers`: 并行处理的进程数（默认：4）

### 3. 查看结果

输出的CSV文件包含以下列：

| 列名 | 说明 | 示例 |
|------|------|------|
| project_name | 项目名称 | killme2008/aviatorscript |
| test_full_name | 测试用例全名 | com.googlecode.aviator.AviatorTest#testAdd |
| oracle_length | 测试预言长度（行数） | 15 |
| assertion_count | 断言数量 | 3 |
| mock_verify_count | Mock验证次数 | 2 |
| uses_mock | 是否使用Mock | true/false |
| called_project_methods | 调用的项目内方法（JSON） | ["com.example.Util.method1", ...] |

## 运行单个测试用例

根据CSV中的 `test_full_name`，你可以使用Maven运行单个测试：

```bash
cd /path/to/project
mvn test -Dtest=com.googlecode.aviator.AviatorTest#testAdd
```

## 工作原理

### 1. 项目发现

- 使用 `mvn help:evaluate` 获取测试和源码目录
- 支持Maven多模块项目（递归查找所有pom.xml）
- 回退到默认路径：`src/test/java` 和 `src/main/java`

### 2. 包名识别

- 扫描 `src/main/java` 下的所有Java文件
- 提取 `package` 声明
- 建立项目包名集合（包括父包）

### 3. 测试用例识别

- 查找带有 `@Test` 注解的方法
- 支持JUnit 4 (`org.junit.Test`) 和 JUnit 5 (`org.junit.jupiter.api.Test`)

### 4. 代码分析

使用 `javalang` 库将Java代码解析为AST（抽象语法树）：

- **测试预言长度**：
  1. 查找方法体中第一个断言语句的行号
  2. 统计方法开始到第一个断言之间的有效代码行数
  3. 排除空行、单行注释（`//`）和块注释（`/* */`）

- **断言统计**：
  - 识别JUnit断言方法：`assertEquals`, `assertTrue`, `assertThat` 等
  - 递归展开private方法后统计

- **Mock检测**：
  - 检查导入：是否有 `mockito` 相关包
  - 检查注解：`@Mock`, `@InjectMocks`
  - 检查方法调用：`mock()`, `when()`, `verify()` 等

- **Private方法展开**：
  1. 识别测试类中的所有private方法
  2. 递归展开private方法调用
  3. 避免循环递归（记录已展开的方法）

- **项目内方法识别**：
  1. 提取方法调用的完整类名
  2. 与项目包名集合对比
  3. 匹配则记录为项目内方法

## 日志

程序运行时会生成两份日志：

1. **控制台输出**：显示INFO级别及以上的日志
2. **文件日志**：`maven_test_metrics.log`，包含详细的DEBUG信息

## 已知限制

1. **类型推断**：对于 `obj.method()` 形式的调用，目前无法完全推断 `obj` 的类型
   - 解决方案：主要依赖静态方法调用 `ClassName.method()`

2. **复杂继承**：暂不支持追踪父类和接口的方法调用

3. **Lambda表达式**：lambda中的断言可能无法准确定位行号

4. **动态Mock**：运行时动态创建的Mock对象可能无法检测

5. **反射调用**：通过反射调用的项目方法无法识别

## 故障排除

### Maven命令超时

如果Maven命令执行缓慢，可以：
- 增加超时时间（修改代码中的 `timeout=30`）
- 确保项目的pom.xml正确配置
- 检查Maven本地仓库是否正常

### 解析错误

如果某些Java文件解析失败：
- 检查Java语法是否正确
- 确认使用的是标准Java语法（不是Groovy等其他JVM语言）
- 查看日志文件了解具体错误

### 内存不足

如果处理大型项目时内存不足：
- 减少 `--workers` 参数值
- 分批处理项目

## 示例输出

```csv
project_name,test_full_name,oracle_length,assertion_count,mock_verify_count,uses_mock,called_project_methods
killme2008/aviatorscript,com.googlecode.aviator.AviatorTest#testAdd,12,2,0,false,"[""com.googlecode.aviator.AviatorEvaluator.execute""]"
killme2008/aviatorscript,com.googlecode.aviator.AviatorTest#testMock,8,1,2,true,"[""com.googlecode.aviator.runtime.RuntimeUtils.getType""]"
```

## 性能

- **小型项目**（<100测试）：通常<1分钟
- **中型项目**（100-1000测试）：1-5分钟
- **大型项目**（>1000测试）：5-30分钟

并行处理可以显著提升速度（建议workers=CPU核心数）。

## 贡献

欢迎提Issue和PR！

## License

MIT License
