# Fund NAV Public

公开基金正式净值查询器。通过 GitHub Actions 定时查询指定基金的最新**正式单位净值**及净值日期，并输出 JSON/CSV。

## 数据口径

- 只记录正式单位净值，不把盘中估值 `gsz` 当作正式净值。
- 主数据源：东方财富 `pingzhongdata/{code}.js` 的 `Data_netWorthTrend`。
- 交叉校验/降级：天天基金 `fundgz.1234567.com.cn` 的 `dwjz + jzrq`。
- 每条结果保留 `nav_date`。QDII 如果尚未披露当日净值，会如实保留较早的净值日期。
- 如果两个来源对同一净值日期给出不同数值，会标记 `source_conflict=true`。

## 输出

- `data/latest_nav.json`
- `data/latest_nav.csv`
- `data/history/YYYY-MM-DD.json`

## 定时任务

工作日北京时间 23:30 自动运行，也可以在 GitHub Actions 页面手动运行。

> 数据仅用于个人研究与数据校验，不构成投资建议。
