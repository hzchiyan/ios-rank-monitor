# 命令与数据接口

脚本自行定位仓库。默认正式模式，测试加`--mode test`，历史完全分离。TOKEN是本轮控制令牌，不是七麦密码；丢失可直接在本机读取`.local/tokens/RUN_ID`使用，不复制到报告。

```sh
python3 scripts/monitor.py doctor
python3 scripts/monitor.py start
python3 scripts/monitor.py start --mode test
python3 scripts/monitor.py pending RUN_ID
python3 scripts/monitor.py resume RUN_ID --token TOKEN
python3 scripts/monitor.py ingest RUN_ID --token TOKEN --input data/page.json
python3 scripts/monitor.py mark RUN_ID --token TOKEN --input data/failure.json
python3 scripts/monitor.py finalize RUN_ID --token TOKEN
python3 scripts/monitor.py render
```

`ingest`/`mark`省略`--input`从标准输入读取JSON。在工具编程环境写JSON，再调用命令，避免把网页文本拼进shell。正式采集使用实际时间；`--at`只用于独立测试，不能用来伪造错过时点。

`start`返回`runId, token, relativePath, previousRunId, plannedScopes`。

## ingest

将`pending`某scope与浏览器实际结果合并，增加真实`capturedAt`（带时区ISO8601）。scope字段为`country, categoryFamily, category, sourceCategory, genreId, chart, device, sourceUrl`。页面字段为`title, sourceDate, completeTop80, rows`。

每行字段：`position`（整数）、`name`、`qimaiUrl`、`developer`、`rating`、`ratingCount`、`latestVersionDate`；可附实际`ranks:[{rank,category}]`用于核对页面分类名次。`qimaiUrl`须为完整`https://www.qimai.cn/app/rank/appid/数字ID/country/国家`。

必须完整80行、position连续1..80、App ID不重复；程序从链接提取App ID。标题、日期、来源URL从实际页面获得，不能为通过校验填预期值。`assets/read-qimai.js`返回可直接合并的页面字段。名称遮罩如`***`照录，缺失基础字段为空。

## mark

```json
{"scope":{"country":"us","categoryFamily":"应用","genreId":"6007","chart":"free"},"status":"source_no_data","reason":"当前日期暂无榜单数据"}
```

状态允许`source_no_data`、`failed`、`auth_required`、`interrupted`。只有网页明确无数据才能用`source_no_data`，空白/加载中不算。原因用不含秘密的简短描述。成功范围不可覆盖，重发相同快照幂等接受。

登录整体失败时，为未完成范围标`auth_required`再finalize；也可直接finalize，剩余范围按失败封存，但最终说明必须写明登录原因。
