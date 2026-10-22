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

正常采集按scanFamilies生成计划，当前仅应用共144范围；缺少开关的旧配置默认为应用与游戏共240范围。新浏览器轮次manifest记录comparisonVersion=shared_scopes_v1，相邻公共范围的排名规则、设备、前80门槛、时区和分类定义一致时继续比较。旧轮次无该字段时沿用整体指纹规则。运行中变更开关会被配置指纹校验拒绝。import-legacy按完整目录导入，不裁剪旧游戏快照。

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

## 首次发布日期补查（新规则轮次）

```sh
python3 scripts/monitor.py prepare-release RUN_ID --token TOKEN
python3 scripts/monitor.py ingest-release RUN_ID --token TOKEN --input data/release.json
```

`start`额外返回固定`deadlineAt`。新轮次manifest保存独立`releasePolicy`、北京时间参考日和1～6个月起始日期；排名比较版本及指纹不因上线筛选改变。旧轮次和legacy_import保留原规则，不能调用日期补查接口。

`prepare-release`在排名候选中按国家＋App ID去重，将已核实缓存固化到本轮`releases.json`，返回`pending`（country/appId/name/sourceUrl）、`candidateApps`和`verifiedApps`。允许重复调用；无候选则pending为空，不额外访问网页。

`ingest-release`省略`--input`时读标准输入。以下仅为接口格式示例，日期、文本和时间必须使用实际页面证据与实际核实时间：

```json
{"country":"us","appId":"123","pageAppId":"123","pageCountry":"美国","status":"verified","releaseDate":"2026-10-01","sourceUrl":"https://www.qimai.cn/app/rank/appid/123/country/us","sourceLabel":"最早发布","rawText":"最早发布 2026-10-01","observedAt":"2026-10-20T04:10:00Z"}
```

verified必须校验可见App ID、国家、明确的最早发布标签及与原文一致的合法日期。状态也支持unavailable、failed，此时releaseDate为空并必须有reason。晚于本轮参考日的日期会转为待核实，不能作为近期发布入选。来源URL须与候选App ID、国家一致；核实时间须位于本轮窗口。相同证据幂等接受；同轮已核实证据不能静默覆盖，结束后不可写入。

`finalize`会固化尚未核实的候选，日期不完整时将新规则轮次记为partial；排名覆盖数不因此减少。新报告counts增加rankCandidates、excludedOlder、pendingRelease；entries只含近6个自然月的已核实候选，releasePending单列未知记录。所有候选都未核实时confirmedNew为null，零排名候选且有可比较范围时为0。重建仅使用每轮固化证据，不用后来缓存回写历史。

报告展示层将无排名比较依据的数量显示为0，并保留覆盖与待比较说明；report-data.json中的null不改写为0。已有排名候选但日期全部未知时，展示层仍显示待核实。

日期补查次数保存在本轮记录中。prepare-release的pending项包含attempts和attemptsRemaining；首次尝试加最多2次重试，累计用尽后不再返回到pending，exhaustedApps说明仍未核实的耗尽数量。重置或resume不清零，完全相同证据重发不增加次数。正式轮次超时后prepare-release/ingest-release拒绝继续补查，仍可finalize；resume返回deadlineAt与windowExpired，过期立即归档。

## 可选QQ邮件

```sh
python3 scripts/monitor.py mail-check
python3 scripts/monitor.py mail-preview RUN_ID
python3 scripts/monitor.py mail-preview TEST_RUN_ID --mode test
python3 scripts/monitor.py mail-send RUN_ID
```

mail-check不创建轮次、不访问SMTP，只检查`.local/mail.json`及权限600的`.local/mail-secrets.json`，返回脱敏地址。enabled默认false；禁用时不要求账号和凭据完整。启用时fromAddress为完整邮箱，recipients为非空邮箱列表；fromName默认“iOS 榜单监测”；smtp可省略，默认host=smtp.qq.com、port=465、security=ssl，username若提供必须等于fromAddress。凭据authCode为QQ SMTP授权码，禁止输出。邮件设置不影响排名配置指纹。

mail-preview接受formal/test的已归档轮次，不发信，返回runId/status/html/text，路径为项目相对路径。mail-send只接受已封存的正式browser轮次；不能自动从测试区推断目标，不能发送legacy_import。调用必须显式指定本轮runId，不遍历历史。

首次发送固化本轮mail/content.json（subject/html/text与内容摘要），发送状态在mail/delivery.json按收件人独立保存。重试、配置变更、模板升级不改变已冻结正文；预览也复用冻结正文。若需要不同内容，不删除发送记录来制造重发。每个收件人是独立的multipart/alternative邮件，只有纯文本和HTML正文，不附文件，不抄送其他收件人。

返回status：not_configured、disabled、busy、submitted、partial、failed或unknown；recipients包含脱敏地址、各自status/reason/skipped；record指向本轮发送记录。submitted只是SMTP服务器已接受，不表示实际收信。已submitted/unknown的收件人跳过；failed可通过再次显式调用重试。进程遗留sending/submitting在下次调用转unknown，禁止自动重发。只有建立连接前的临时网络错误自动重试一次；认证、TLS证书、明确拒收、提交后异常不自动重试。

收件人身份只将域名规范化为小写，保留@前部分的大小写；兼容已有发送记录中的大写域名，重复身份或损坏状态停止发送并提示人工核对。DATA后的畸形回复、本地合成的SMTP异常不能证明拒收，归unknown；只有可信的明确拒收才能记failed。

配置错误返回脱敏error且退出非0；SMTP发送结果partial/failed/unknown/busy退出1，其余退出0。任务既要读取退出码，也要读取JSON状态，邮件异常不能引起重采集、重建替代轮次或破坏归档。finalize/render/import均无SMTP副作用。
