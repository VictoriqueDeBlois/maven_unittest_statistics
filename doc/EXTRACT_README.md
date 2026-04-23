# 测试用例代码片段提取工具

## 功能说明

该工具用于从CSV文件中读取测试用例信息，然后从源代码仓库中提取对应的测试方法代码片段，保存到单独的文件中。

## 主要功能

### 1. 提取前N个指标最大的测试用例

根据指定的指标（测试预言长度、断言数量、Mock验证次数）排序，提取前N个测试用例的代码。

### 2. 提取指定的测试用例

根据测试用例全名列表，提取特定的测试用例代码。

## 使用方法

### 模式1：提取前100个测试预言长度最大的用例

```bash
python3 extract_test_snippets.py \
    --csv test_metrics.csv \
    --root /path/to/repos \
    --output extracted_tests \
    --mode top \
    --top-n 100 \
    --sort-by oracle_length
```

### 模式2：提取前50个断言数量最多的用例

```bash
python3 extract_test_snippets.py \
    --csv test_metrics.csv \
    --root /path/to/repos \
    --output extracted_tests \
    --mode top \
    --top-n 50 \
    --sort-by assertion_count
```

### 模式3：提取前30个Mock验证最多的用例

```bash
python3 extract_test_snippets.py \
    --csv test_metrics.csv \
    --root /path/to/repos \
    --output extracted_tests \
    --mode top \
    --top-n 30 \
    --sort-by mock_verify_count
```

### 模式4：提取指定的测试用例

```bash
python3 extract_test_snippets.py \
    --csv test_metrics.csv \
    --root /path/to/repos \
    --output extracted_tests \
    --mode specific \
    --test-names \
        "me.zhyd.oauth.utils.GlobalAuthUtilsTest#md5" \
        "com.example.MyTest#testMethod"
```

## 参数说明

| 参数 | 必需 | 说明 | 默认值 |
|------|------|------|--------|
| `--csv` | 是 | CSV文件路径 | - |
| `--root` | 是 | 所有项目的根目录 | - |
| `--output` | 是 | 输出目录 | - |
| `--mode` | 否 | 模式：top或specific | top |
| `--top-n` | 否 | 提取前N个用例（mode=top时） | 100 |
| `--sort-by` | 否 | 排序字段：oracle_length、assertion_count、mock_verify_count | oracle_length |
| `--test-names` | 否 | 测试用例全名列表（mode=specific时） | - |

## 输出文件命名规则

提取的代码片段文件命名格式：

```
{项目名}__{类名}__{方法名}.java
```

例如：
- `justauth_JustAuth__GlobalAuthUtilsTest__md5.java`
- `apache_commons-lang__StringUtilsTest__testIsEmpty.java`

## 输出文件内容

每个文件包含：

1. **文件头注释**：包含项目信息和测试指标
   ```java
   // Project: justauth/JustAuth
   // Test: me.zhyd.oauth.utils.GlobalAuthUtilsTest#md5
   // Oracle Length: 2
   // Assertions: 1
   // Mock Verifications: 0
   // Uses Mock: False
   // Called Methods: ["me.zhyd.oauth.utils.GlobalAuthUtils.md5"]
   ```

2. **完整的测试方法代码**：包括注解、方法签名和方法体
   ```java
   @Test
   public void md5() {
       String str = "helloworld,iamjustauth";
       String md5Str = GlobalAuthUtils.md5(str);
       assertEquals("b0d923de4289b69976448cac718528b8", md5Str);
   }
   ```

## 工作原理

### 1. 查找测试文件

- 根据项目名在根目录下定位项目
- 根据完整类名（如 `com.example.MyTest`）转换为文件路径（`com/example/MyTest.java`）
- 在所有可能的测试目录中查找：
  - `src/test/java`
  - `test`
  - `tests`
- 支持多模块Maven项目（递归查找所有pom.xml）

### 2. 提取方法代码

- 使用正则表达式匹配方法声明（支持注解）
- 通过大括号匹配确定方法体范围
- 提取完整的方法代码（包括注解）

### 3. 保存文件

- 创建输出目录（如不存在）
- 生成安全的文件名（替换非法字符）
- 添加文件头注释
- 保存代码片段

## 使用场景

### 场景1：分析测试预言长度最长的用例

```bash
# 提取前100个测试预言长度最大的用例
python3 extract_test_snippets.py \
    --csv test_metrics.csv \
    --root /data/repos \
    --output analysis/long_oracle \
    --mode top \
    --top-n 100 \
    --sort-by oracle_length
```

**用途**：分析哪些测试用例在断言前有大量的设置代码，可能存在优化空间。

### 场景2：研究断言密集的测试

```bash
# 提取断言数量最多的测试
python3 extract_test_snippets.py \
    --csv test_metrics.csv \
    --root /data/repos \
    --output analysis/many_assertions \
    --mode top \
    --top-n 50 \
    --sort-by assertion_count
```

**用途**：研究哪些测试使用了大量断言，分析测试粒度和质量。

### 场景3：分析Mock使用模式

```bash
# 提取Mock验证最多的测试
python3 extract_test_snippets.py \
    --csv test_metrics.csv \
    --root /data/repos \
    --output analysis/heavy_mock \
    --mode top \
    --top-n 30 \
    --sort-by mock_verify_count
```

**用途**：研究复杂的Mock使用场景，分析测试的隔离性。

### 场景4：案例研究

```bash
# 提取特定的测试用例进行详细分析
python3 extract_test_snippets.py \
    --csv test_metrics.csv \
    --root /data/repos \
    --output case_studies \
    --mode specific \
    --test-names \
        "me.zhyd.oauth.utils.GlobalAuthUtilsTest#md5" \
        "me.zhyd.oauth.utils.GlobalAuthUtilsTest#urlEncode"
```

**用途**：深入研究特定的测试用例。

## 错误处理

程序会记录以下情况的日志：

- **项目路径不存在**：跳过该测试用例
- **测试文件未找到**：跳过该测试用例
- **方法代码提取失败**：跳过该测试用例（可能是方法名不匹配或大括号未闭合）
- **文件保存失败**：记录错误但继续处理其他用例

所有日志会输出到控制台，包括：
- INFO级别：正常进度信息
- WARNING级别：跳过的用例和原因
- ERROR级别：严重错误

## 编程接口

如果需要在Python脚本中使用，可以直接调用函数：

```python
from pathlib import Path
from extract_test_snippets import extract_top_n_test_cases, extract_test_case_code

# 提取前100个测试预言长度最大的用例
extract_top_n_test_cases(
    csv_file=Path('test_metrics.csv'),
    projects_root=Path('/data/repos'),
    output_dir=Path('extracted_tests'),
    top_n=100,
    sort_by='oracle_length'
)

# 提取单个测试用例
csv_row = {
    'project_name': 'justauth/JustAuth',
    'test_full_name': 'me.zhyd.oauth.utils.GlobalAuthUtilsTest#md5',
    'oracle_length': '2',
    'assertion_count': '1',
    'mock_verify_count': '0',
    'uses_mock': 'False',
    'called_project_methods': '["me.zhyd.oauth.utils.GlobalAuthUtils.md5"]'
}

extract_test_case_code(
    csv_row=csv_row,
    projects_root=Path('/data/repos'),
    output_dir=Path('extracted_tests')
)
```

## 注意事项

1. **文件编码**：假定所有Java文件使用UTF-8编码
2. **方法匹配**：使用正则表达式匹配方法，可能在某些复杂情况下失败
3. **大括号匹配**：通过简单的计数匹配大括号，不处理字符串中的大括号
4. **项目结构**：假定标准的Maven项目结构
5. **性能**：对于大量测试用例，提取过程可能需要一些时间

## 示例输出

运行后，输出目录结构如下：

```
extracted_tests/
├── justauth_JustAuth__GlobalAuthUtilsTest__md5.java
├── justauth_JustAuth__GlobalAuthUtilsTest__urlEncode.java
├── apache_commons-lang__StringUtilsTest__testIsEmpty.java
└── ...
```

每个文件包含完整的测试方法代码和元数据注释。
