# 公共建筑改造复核

用于公共机构安排下一年度建筑节能改造资金的本地分析应用。它把**已完成改造的实测表现**与**下一年度候选措施**分开排序，并坚持三条底线：

1. **节能必须建立在可比口径上** —— 用途、开放时间、气象变化先修正或直接判为不可比，电表更换、租户变化、其他设备调试作为混杂因素单独登记。
2. **服务不达标不能用节能抵消** —— 舒适度抽测不足时只给区间和"证据不足"，不给单点结论；满意率区间上界仍低于用途目标时，项目不进入优先排序。
3. **审批依据可追溯** —— 批准时冻结当时的比较期、能耗、舒适度区间与理由；之后补录/更正只追加差异，并列出受影响的排序和未完成事项。

## 运行

需要 Python 3.11 或更新版本，仅使用标准库。

```bash
python -m retrofit.server --workspace state/workspace.json --seed demo_data
# 打开 http://127.0.0.1:8000/
```

首次启动会从 `--seed` 目录导入 CSV 并保存到 workspace。`data/` 是随仓库提供的最小样例；`demo_data/` 是更完整的演示数据集（4 栋建筑、4 个已完成项目、3 个候选措施，覆盖用途变更、电表更换、样本不足、气象差异等情形）。

也可直接调用分析器：

```bash
python -c "from retrofit.inputs import load_csv_dir; from retrofit.analysis import analyse; \
print(analyse(load_csv_dir('demo_data')).summary)"
```

## 界面分区

| 分区 | 作用 |
|---|---|
| 总览 | 已完成项目的期间能耗对比、分期时间轴与结论 |
| 投资排序 | 已完成项目（按实测）与下一年度候选措施（按预计+参照）分开排序；暂缓/需补测单列 |
| 项目与解释 | 每个项目的结论、理由代码和完整说明，可据此解释优先/暂缓/补测 |
| 登记措施 | 登记措施、施工期、投运期、计划停用时段和预计节能；校验停用与开放目标冲突 |
| 验收与复核 | 批准后移交设施团队；补录实测能耗/舒适度；登记验收并比对原依据 |
| 资料与更正 | 导入 CSV/JSON、登记现场事件、提交更正并查看重算影响 |
| 审批与留痕 | 冻结的批准依据、与当前结论的差异、操作日志 |
| 口径说明 | 比较期划分、修正口径、排序规则的书面说明 |

## 领域规则（对应 `retrofit/analysis.py`）

- **比较期**：基线期 / 改造施工期 / 设备投运期 / 改造后稳定运行期相互独立。投运期结束次日起算改造后比较期；基线优先取上年同期，否则取改造前相邻等长期。
- **用途变化**：基线与改造后用途类型不同 → `incomparable`，不计节能。
- **开放时间变化**：按周开放小时折算后再比较，变化本身不计为设备效率；同时与用途服务目标核对。
- **气象**：按采暖度日比修正并标注；缺少度日时提示未修正。
- **混杂因素**：电表更换、租户变化等落在比较期内时，未核对前结论降为 `needs_measurement`，并列入未完成事项。
- **舒适度**：按样本量加权，90% Wilson 区间；样本不足或覆盖率偏低时区间加宽并标注"证据不足"；区间上界低于目标 → `service_fail`。
- **候选措施**：以改造前近期抽测作为服务基线；不虚构改造后比较期；预计停用超过施工期 25% 或与开放目标冲突时暂缓。
- **排序**：已完成项目按 `年化节能量 × 证据强度`（可除以投资）排序；候选按预计年化节能量 × 同楼业绩参照系数排序。

每个结论都带稳定的理由代码（`verified_saving`、`use_type_change`、`comfort_below_target`、`closure_conflict` 等），便于审计与复现。

## 数据字段

CSV 列名可参考 `demo_data/`。可选列留空即可，逗号后的扩展示例：

- `buildings.csv`：`building_id,use_type,open_days,open_hours_per_day,valid_from,valid_to`
- `energy_weather.csv`：`building_id,period,electric_kwh,gas_m3,degree_days,coverage_pct[,meter_id,note]`（`period` 支持 `2026-Q1`、`2026-03` 或 `2026-01-01/2026-03-31`）
- `comfort_projects.csv`：`record_id,building_id,date,comfort_sample_pct,complaint_count,project_id,stage,effective_date[,sample_count,sample_coverage_pct]`（`stage=commissioning/works` 的抽测不计入稳定期）
- `service_targets.csv`：`use_type,min_open_days,min_open_hours_per_day,min_comfort_sample_pct,valid_from`
- `retrofit_projects.csv`：`project_id,building_id,name,measures,measure_start,measure_end,commissioning_start,commissioning_end,planned_closure_start,planned_closure_end,predicted_saving_pct,cost,status,approved_on,accepted_on`
- `site_events.csv`：`event_id,building_id,event_type,start,end,verified,note`（类型：`meter_replacement`、`tenant_change`、`equipment_commissioning`、`use_change`、`other`）
- `commissioning_measurements.csv`：`measurement_id,project_id,date,electric_kwh,gas_m3,degree_days,comfort_sample_pct,sample_count,note`

JSON 导入采用同名键的对象或对象数组，例如 `{"energy": [...]}`。

## 测试

```bash
python -m unittest discover -s tests -v      # 口径、比较期、服务门槛、工作流、HTTP
npm run test:js                              # 图表与页面渲染（可选；需先 npm install 取 jsdom）
```

Python 部分只依赖标准库。`tests/js/render.test.mjs` 在没有 jsdom 时会自动跳过，不影响应用运行。

## 说明

所有随附记录均为虚构，仅供本地开发与复核演练。应用不联网，状态保存在本地 JSON 文件。
