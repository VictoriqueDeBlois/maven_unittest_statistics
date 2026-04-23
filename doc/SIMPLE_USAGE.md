# 简单使用说明

按下面顺序调用即可：

## 1. 汇总全部单测信息

先用 `maven_test_metrics.py` 生成总表：

```bash
python3 maven_test_metrics.py \
  --projects repos.txt \
  --root /path/to/repos \
  --output test_metrics.csv
```

## 2. 筛选名字里像集成测试的用例

再用 `filter_integration_tests.py` 从总表里筛出集成测试：

```bash
python3 filter_integration_tests.py \
  --input test_metrics.csv \
  --output integration_tests.csv
```

## 3. 抽取对应测试代码

最后用 `extract_test_snippets.py` 抽取筛选结果对应的测试代码：

```bash
python3 extract_test_snippets.py \
  --csv integration_tests.csv \
  --root /path/to/repos \
  --output extracted_integration_tests \
  --mode all
```

## 说明

- `--root` 指所有 Maven 项目的根目录。
- `repos.txt` 里每行一个项目名，和 `maven_test_metrics.py` 的 `--root` 配合使用。
- 第一步输出总表 `test_metrics.csv`。
- 第二步输出筛选后的 `integration_tests.csv`。
- 第三步会把每个测试方法保存成单独的 `.java` 文件。
