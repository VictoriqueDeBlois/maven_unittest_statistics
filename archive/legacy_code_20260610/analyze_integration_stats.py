"""
统计 all_integration_code.csv 中各字段的分布并绘图
字段: oracle_length, assertion_count, mock_verify_count, uses_mock, called_project_methods(方法数量)
"""

import ast
import csv
import os

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
from matplotlib.gridspec import GridSpec

# ── 配置 ──────────────────────────────────────────────────────────────────────
CSV_PATH = "all_integration_code.csv"
OUTPUT_DIR = "results"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 尝试使用支持中文的字体，fallback 到默认字体
import matplotlib.font_manager as fm
_zh_candidates = ["WenQuanYi Micro Hei", "Noto Sans CJK SC", "SimHei", "Microsoft YaHei", "DejaVu Sans"]
_available = {f.name for f in fm.fontManager.ttflist}
_font_family = next((f for f in _zh_candidates if f in _available), "DejaVu Sans")

plt.rcParams.update({
    "figure.dpi": 150,
    "font.size": 11,
    "axes.titlesize": 13,
    "axes.labelsize": 11,
    "font.family": _font_family,
    "axes.unicode_minus": False,
})

# ── 读取数据 ───────────────────────────────────────────────────────────────────
print("正在读取数据 ...")
df = pd.read_csv(CSV_PATH)
total = len(df)
print(f"总记录数: {total:,}")

# 解析 called_project_methods → 方法数量
def parse_method_count(val):
    if pd.isna(val) or str(val).strip() in ("", "[]"):
        return 0
    try:
        lst = ast.literal_eval(str(val))
        return len(lst) if isinstance(lst, list) else 0
    except Exception:
        # 简单 fallback：按逗号计数
        return str(val).count(",") + 1

df["method_count"] = df["called_project_methods"].apply(parse_method_count)

# uses_mock → bool
df["uses_mock_bool"] = df["uses_mock"].astype(str).str.strip().str.lower() == "true"

# ── 文字统计摘要 ───────────────────────────────────────────────────────────────
numeric_cols = {
    "oracle_length":      "Oracle Length",
    "assertion_count":    "Assertion Count",
    "mock_verify_count":  "Mock Verify Count",
    "method_count":       "Called Project Methods (count)",
}

print("\n" + "=" * 60)
print("  数值字段统计摘要")
print("=" * 60)
for col, label in numeric_cols.items():
    s = df[col]
    print(f"\n[{label}]")
    print(f"  总数        : {total:,}")
    print(f"  最小值      : {s.min()}")
    print(f"  最大值      : {s.max()}")
    print(f"  平均值      : {s.mean():.4f}")
    print(f"  中位数      : {s.median():.1f}")
    print(f"  标准差      : {s.std():.4f}")
    print(f"  值为 0 的比例: {(s == 0).sum() / total * 100:.2f}%")

mock_true_pct = df["uses_mock_bool"].sum() / total * 100
print(f"\n[Uses Mock]")
print(f"  True  : {df['uses_mock_bool'].sum():,}  ({mock_true_pct:.2f}%)")
print(f"  False : {(~df['uses_mock_bool']).sum():,}  ({100 - mock_true_pct:.2f}%)")
print("=" * 60)

# ── 绘图函数 ───────────────────────────────────────────────────────────────────

def hist_with_stats(ax, series, title, xlabel, color, max_val_pct=0.99):
    """带均值/中位数标注的直方图（截断长尾）"""
    cap = series.quantile(max_val_pct)
    data = series[series <= cap]
    clipped = len(series) - len(data)

    bins = min(int(cap) + 1, 60) if cap <= 60 else 60
    ax.hist(data, bins=bins, color=color, edgecolor="white", linewidth=0.4, alpha=0.85)

    mean_val = series.mean()
    med_val  = series.median()
    ax.axvline(mean_val, color="crimson",  linestyle="--", linewidth=1.4, label=f"Mean={mean_val:.2f}")
    ax.axvline(med_val,  color="navy",     linestyle=":",  linewidth=1.4, label=f"Median={med_val:.1f}")

    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("Count")
    ax.legend(fontsize=9)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{int(x):,}"))

    if clipped > 0:
        ax.text(0.98, 0.97, f"Trimmed {clipped} extreme values (>{cap:.0f})",
                transform=ax.transAxes, ha="right", va="top",
                fontsize=8, color="gray")


def bar_value_counts(ax, series, title, color, top_n=15):
    """Top-N 值频次柱状图"""
    vc = series.value_counts().sort_index()
    if len(vc) > top_n:
        vc = vc.head(top_n)
    ax.bar(vc.index.astype(str), vc.values, color=color, edgecolor="white", linewidth=0.4, alpha=0.85)
    ax.set_title(title)
    ax.set_xlabel("Value")
    ax.set_ylabel("Count")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{int(x):,}"))
    for x, y in zip(range(len(vc)), vc.values):
        ax.text(x, y + total * 0.002, f"{y:,}", ha="center", va="bottom", fontsize=7.5)


# ── 图 1：四个数值字段分布（2×2）────────────────────────────────────────────────
colors = ["#4C72B0", "#DD8452", "#55A868", "#C44E52"]
fig1, axes1 = plt.subplots(2, 2, figsize=(14, 10))
fig1.suptitle("Integration Test Numeric Fields Distribution", fontsize=15, fontweight="bold", y=1.01)

field_info = [
    ("oracle_length",     "Oracle Length Distribution",             "oracle_length",     colors[0]),
    ("assertion_count",   "Assertion Count Distribution",           "assertion_count",   colors[1]),
    ("mock_verify_count", "Mock Verify Count Distribution",         "mock_verify_count", colors[2]),
    ("method_count",      "Called Project Methods Count Distribution","method_count",    colors[3]),
]

for ax, (col, title, xlabel, clr) in zip(axes1.flat, field_info):
    hist_with_stats(ax, df[col], title, xlabel, clr)

fig1.tight_layout()
out1 = os.path.join(OUTPUT_DIR, "numeric_distributions.png")
fig1.savefig(out1, bbox_inches="tight")
print(f"\n已保存: {out1}")

# ── 图 2：uses_mock 饼图 + 柱状图 ───────────────────────────────────────────────
fig2, (ax_pie, ax_bar) = plt.subplots(1, 2, figsize=(12, 5))
fig2.suptitle("Uses Mock Distribution", fontsize=15, fontweight="bold")

mock_counts = df["uses_mock_bool"].value_counts()
labels = ["False (No Mock)", "True (Uses Mock)"]
vals   = [mock_counts.get(False, 0), mock_counts.get(True, 0)]
pie_colors = ["#AED6F1", "#F1948A"]
explode    = (0, 0.05)

ax_pie.pie(vals, labels=labels, colors=pie_colors, explode=explode,
           autopct="%1.2f%%", startangle=140,
           textprops={"fontsize": 11}, pctdistance=0.82)
ax_pie.set_title("Proportion (Pie)")

ax_bar.bar(labels, vals, color=pie_colors, edgecolor="white", width=0.5)
ax_bar.set_title("Count (Bar)")
ax_bar.set_ylabel("Count")
ax_bar.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{int(x):,}"))
for i, v in enumerate(vals):
    ax_bar.text(i, v + total * 0.003, f"{v:,}\n({v/total*100:.1f}%)",
                ha="center", va="bottom", fontsize=10)

fig2.tight_layout()
out2 = os.path.join(OUTPUT_DIR, "uses_mock_distribution.png")
fig2.savefig(out2, bbox_inches="tight")
print(f"已保存: {out2}")

# ── 图 3：各字段值频次详细柱状图（Top-15）────────────────────────────────────────
fig3, axes3 = plt.subplots(2, 2, figsize=(16, 10))
fig3.suptitle("Top-15 Value Frequency (Numeric Fields)", fontsize=15, fontweight="bold", y=1.01)

for ax, (col, title, _, clr) in zip(axes3.flat, field_info):
    bar_value_counts(ax, df[col], f"{title}\n(Top-15 values)", clr)

fig3.tight_layout()
out3 = os.path.join(OUTPUT_DIR, "value_frequency.png")
fig3.savefig(out3, bbox_inches="tight")
print(f"已保存: {out3}")

# ── 图 4：综合 Overview（CDF + 箱线图）──────────────────────────────────────────
fig4 = plt.figure(figsize=(16, 10))
gs   = GridSpec(2, 2, figure=fig4, hspace=0.35, wspace=0.3)
fig4.suptitle("Cumulative Distribution & Boxplot Overview", fontsize=15, fontweight="bold")

num_cols = ["oracle_length", "assertion_count", "mock_verify_count", "method_count"]
num_labels = ["Oracle Length", "Assertion Count", "Mock Verify Count", "Called Methods Count"]

# CDF
ax_cdf = fig4.add_subplot(gs[0, :])
for col, label, clr in zip(num_cols, num_labels, colors):
    s = df[col].sort_values()
    cdf = np.arange(1, len(s) + 1) / len(s)
    cap = s.quantile(0.98)
    mask = s <= cap
    ax_cdf.plot(s[mask], cdf[mask], label=label, color=clr, linewidth=1.8)
ax_cdf.set_title("Cumulative Distribution Function (CDF, trimmed at 98th percentile)")
ax_cdf.set_xlabel("Value")
ax_cdf.set_ylabel("Cumulative Proportion")
ax_cdf.legend()
ax_cdf.grid(True, alpha=0.3)

# 箱线图（去除 0 以观察非零分布）
ax_box_all  = fig4.add_subplot(gs[1, 0])
ax_box_nonz = fig4.add_subplot(gs[1, 1])

data_all  = [df[c].values for c in num_cols]
data_nonz = [df[df[c] > 0][c].values for c in num_cols]

bp1 = ax_box_all.boxplot(data_all, patch_artist=True, notch=False, showfliers=False)
bp2 = ax_box_nonz.boxplot(data_nonz, patch_artist=True, notch=False, showfliers=False)

for bp, axes_obj in [(bp1, ax_box_all), (bp2, ax_box_nonz)]:
    for patch, clr in zip(bp["boxes"], colors):
        patch.set_facecolor(clr)
        patch.set_alpha(0.7)

ax_box_all.set_xticklabels(num_labels, rotation=15, ha="right")
ax_box_all.set_title("Boxplot (All, no outliers)")
ax_box_all.set_ylabel("Value")

ax_box_nonz.set_xticklabels(num_labels, rotation=15, ha="right")
ax_box_nonz.set_title("Boxplot (Non-zero only, no outliers)")
ax_box_nonz.set_ylabel("Value")

out4 = os.path.join(OUTPUT_DIR, "cdf_and_boxplot.png")
fig4.savefig(out4, bbox_inches="tight")
print(f"已保存: {out4}")

# ── 图 5：uses_mock × 各数值字段对比（小提琴图）─────────────────────────────────
fig5, axes5 = plt.subplots(1, 4, figsize=(18, 6), sharey=False)
fig5.suptitle("Numeric Fields vs Uses Mock (Violin Plot, trimmed at 99th pct)", fontsize=14, fontweight="bold")

for ax, col, label, clr in zip(axes5, num_cols, num_labels, colors):
    cap = df[col].quantile(0.99)
    grp_false = df[~df["uses_mock_bool"] & (df[col] <= cap)][col].values
    grp_true  = df[ df["uses_mock_bool"] & (df[col] <= cap)][col].values
    parts = ax.violinplot([grp_false, grp_true], positions=[0, 1],
                          showmedians=True, showextrema=True)
    for pc in parts["bodies"]:
        pc.set_alpha(0.7)
    ax.set_xticks([0, 1])
    ax.set_xticklabels(["No Mock", "Uses Mock"])
    ax.set_title(label)
    ax.set_ylabel("Value")

fig5.tight_layout()
out5 = os.path.join(OUTPUT_DIR, "violin_by_mock.png")
fig5.savefig(out5, bbox_inches="tight")
print(f"已保存: {out5}")

print("\n全部图表已保存到 results/ 目录。")
plt.close("all")
