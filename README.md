# Fund NAV Public

针对**指定若干只基金**的正式单位净值采集器。使用 GitHub Actions 定时查询，并把结果提交回仓库，供浏览器、脚本或 ChatGPT 读取。

## 配置方式

`funds.json` 只维护基金代码，不再手工维护基金名称：

```json
{
  "funds": [
    "000218",
    "162412",
    "005051"
  ]
}
```

程序会从公开基金数据中自动解析名称。增加或删除长期关注基金时，只需要修改这里的 6 位基金代码。

当前配置是 44 只持仓基金。仓库只保存公开基金代码、名称和公开净值，不保存账户、金额或其他隐私数据。

## 数据口径

- 只记录**正式单位净值**，不把盘中估值 `gsz` 当作正式净值。
- 主源：东方财富/天天基金 `pingzhongdata/{code}.js` 的历史正式净值。
- 交叉校验/降级：`fundgz.1234567.com.cn` 的 `dwjz + jzrq`。
- 每条记录都保留 `nav_date`，因此 QDII/T+1/T+2 披露延迟不会被误认为当天净值。
- 同一日期多个来源数值不一致时，标记 `source_conflict=true`。

## 定时查询

工作日北京时间 **18:37、20:37、22:37、23:37** 自动查询 `funds.json` 中配置的基金。

输出：

- `data/latest_nav.json`
- `data/latest_nav.csv`
- `data/history/YYYY-MM-DD.json`

Raw JSON：

```
https://raw.githubusercontent.com/bvans/fund-nav-public/main/data/latest_nav.json
```

## 手动临时查询任意若干只基金

进入：

**Actions → Fetch official fund NAV → Run workflow**

在 `codes` 中输入任意若干基金代码，例如：

```
000218,162412,005051
```

也支持空格或换行分隔。

如果 `codes` 留空，则仍然查询 `funds.json`。

手动指定代码时，结果单独写入：

- `data/manual_latest_nav.json`
- `data/manual_latest_nav.csv`

因此不会覆盖日常长期关注列表的 `latest_nav.json`。

## 返回示例

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

## 本地临时查询

查询 `funds.json`：

```bash
python -m pip install -r requirements.txt
python fetch_nav.py
```

临时查询指定基金：

```bash
FUND_CODES="000218,162412,005051" python fetch_nav.py
```

> 数据仅用于个人研究、资产汇总与数据校验，不构成投资建议。
