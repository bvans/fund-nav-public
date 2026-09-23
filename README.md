# Fund NAV Public

针对个人持仓基金的**正式单位净值**采集器。使用 GitHub Actions 的公开仓库 runner 定时查询，并把结果提交回仓库，供浏览器、脚本或 ChatGPT 读取。

## 当前基金

`funds.json` 中维护 44 只基金代码，来自当前持仓清单。仓库只保存公开基金代码、名称和公开净值，不保存账户、金额或其他隐私数据。

## 数据口径

- 只记录**正式单位净值**，不把盘中估值 `gsz` 当作正式净值。
- 主源：东方财富/天天基金 `pingzhongdata/{code}.js` 的历史正式净值。
- 交叉校验/降级：`fundgz.1234567.com.cn` 的 `dwjz + jzrq`。
- 每条记录必须带 `nav_date`，因此 QDII/T+1/T+2 披露延迟不会被误认为当天净值。
- 同一日期多个来源数值不一致时，标记 `source_conflict=true`。
- 数据抓取逻辑参考并验证了 AKShare 的基金接口设计；AKShare 的 `fund_open_fund_daily_em` / `fund_open_fund_info_em` 同样以东方财富/天天基金为目标数据源。

## 输出文件

- `data/latest_nav.json`：最新正式净值，推荐程序读取
- `data/latest_nav.csv`：便于人工检查
- `data/history/YYYY-MM-DD.json`：每天快照

### Raw JSON

```
https://raw.githubusercontent.com/bvans/fund-nav-public/main/data/latest_nav.json
```

示例：

```json
{
  "code": "000218",
  "name": "国泰黄金ETF联接A",
  "nav_date": "2026-09-22",
  "unit_nav": 3.3625,
  "source": "eastmoney:pingzhongdata"
}
```

## GitHub Actions

工作日北京时间 **18:37、20:37、22:37、23:37** 自动刷新，也支持 Actions 页面手动运行。

基金公司披露净值的时间并不统一，多次晚间刷新可以逐步补齐当天净值；QDII 若当天尚未披露，会保留其真实的最新净值日期，而不是伪装成当天数据。

## 本地运行

```bash
python -m pip install -r requirements.txt
python fetch_nav.py
```

> 数据仅用于个人研究、资产汇总与数据校验，不构成投资建议。
