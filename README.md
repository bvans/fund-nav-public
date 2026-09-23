# Fund NAV Public

用于长期保存中国公募基金**正式单位净值**的 GitHub 数据仓库。GitHub Actions 自动采集，结果直接提交回仓库。

当前有两层数据：

1. **全市场历史库**：每天同步全市场开放式基金净值，并按真实净值日期归档。
2. **重点基金增强库**：`funds.json` 中的持仓/关注基金每天多次刷新，并用两个公开来源交叉校验。

仓库只保存公开基金代码、名称、类型和公开净值，不保存账户、持仓金额或其他隐私数据。

## 1. 全市场净值历史

工作流：`.github/workflows/sync-all-nav.yml`

北京时间：

- 工作日 23:47：全量同步
- 次日 02:17：再次同步，补晚披露数据

数据来源：

- AKShare `fund_name_em()`：全市场基金目录
- AKShare `fund_open_fund_daily_em()`：东方财富/天天基金全量开放式基金净值
- 东方财富 `pingzhongdata/{code}.js`：QDII/延迟披露基金最近历史回补

### 历史文件

```text
data/all/
├── latest_nav.json
├── latest_nav.csv.gz
├── fund_catalog.json.gz
└── history/
    └── 2026/
        ├── 2026-09-22.csv.gz
        ├── 2026-09-23.csv.gz
        └── ...
```

历史文件按**净值日期 `nav_date`**存储，而不是抓取日期。

例如某只 QDII 在 9 月 25 日才公布 9 月 23 日净值，程序会把该记录合并回：

```text
data/all/history/2026/2026-09-23.csv.gz
```

因此一年后可以直接按日期读取过去一年每个交易日保存下来的基金净值。

历史 CSV 字段：

- `code`
- `name`
- `type`
- `nav_date`
- `unit_nav`
- `accum_nav`
- `source`

全量历史使用 gzip 压缩，避免一年后仓库体积过快增长。

## 2. 重点基金增强查询

`funds.json` 维护重点基金代码。当前配置为 44 只持仓基金。

工作日北京时间 **18:37、20:37、22:37、23:37** 自动刷新：

- 主源：东方财富/天天基金 `pingzhongdata/{code}.js`
- 校验/降级：`fundgz.1234567.com.cn` 的 `dwjz + jzrq`
- 只使用正式净值，不把盘中估值 `gsz` 当净值
- 每条数据保留 `nav_date`
- 同日两来源冲突时标记 `source_conflict=true`

输出：

```text
data/latest_nav.json
data/latest_nav.csv
data/history/YYYY-MM-DD.json
```

Raw JSON：

```text
https://raw.githubusercontent.com/bvans/fund-nav-public/main/data/latest_nav.json
```

## 3. 临时查询任意基金

进入：

**Actions → Fetch official fund NAV → Run workflow**

在 `codes` 中输入：

```text
000218,162412,005051
```

结果写入：

```text
data/manual_latest_nav.json
data/manual_latest_nav.csv
```

不会覆盖重点基金日常数据。

## 示例

```json
{
  "code": "000218",
  "name": "国泰黄金ETF联接A",
  "nav_date": "2026-09-22",
  "unit_nav": 3.3625,
  "source": "eastmoney:pingzhongdata",
  "source_conflict": false
}
```

## 本地运行

重点基金：

```bash
python -m pip install -r requirements.txt
python fetch_nav.py
```

全市场历史：

```bash
python sync_all_nav.py
```

> 数据仅用于个人研究、资产汇总与数据校验，不构成投资建议。
