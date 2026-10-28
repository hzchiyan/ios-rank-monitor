# 命令与数据接口

脚本自行定位仓库。默认正式模式，测试加`--mode test`，历史完全分离。TOKEN是本次执行权令牌，不是七麦密码，不复制到报告。start领取/接管时返回令牌并保存在`.local/tokens/RUN_ID`。新执行者必须经start领取，不能直接读取别的执行者令牌抢占；工具重置仍持有本次执行权时可用原令牌resume。

```sh
python3 scripts/monitor.py doctor
python3 scripts/monitor.py start
python3 scripts/monitor.py start --mode test
python3 scripts/monitor.py pending RUN_ID
python3 scripts/monitor.py resume RUN_ID --token TOKEN
python3 scripts/monitor.py progress RUN_ID --token TOKEN
python3 scripts/monitor.py suspend RUN_ID --token TOKEN --reason "需要人工验证码"
python3 scripts/monitor.py ingest RUN_ID --token TOKEN --input data/page.json
python3 scripts/monitor.py mark RUN_ID --token TOKEN --input data/failure.json
python3 scripts/monitor.py finalize RUN_ID --token TOKEN
python3 scripts/monitor.py render
```

`ingest`/`mark`省略`--input`从标准输入读取JSON。在工具编程环境写JSON，再调用命令，避免把网页文本拼进shell。正式采集使用实际时间；`--at`只用于独立测试，不能用来伪造错过时点。

`start`返回`runId, token, relativePath, previousRunId, plannedScopes`。新浏览器轮次记录executionPolicy=completion_v1，额外返回固定sourceDate、leaseExpiresAt，没有deadlineAt。已有未结束新轮时优先恢复同runId：执行者仍有15分钟租约则skipped=true且不返回令牌；租约过期、suspend或标记丢失时接管返回resumed=true并轮换令牌，旧令牌立即失效。租约只控制执行权，绝不封存轮次。过期但尚未被接管时原令牌仍可续约；接管轮换令牌后原令牌不可用。恢复完成后同次触发不再start新轮。

resume恢复保存的快照状态及待办，progress返回phase（rankings/releases/blocked/ready）、pending、completedScopes、plannedScopes、release及canFinalize。保存、resume、progress续租；长浏览器操作前后应在15分钟内主动续约。blocked状态的progress仅检查进度、不恢复执行权，resume或下一次start才恢复。suspend记录blocked/lastBlock，保留running并释放执行权，不消耗各项重试，不归档或发邮件。全局登录、工具、网络受阻应使用它，不能批量失败替代。

pending只列新轮尚可执行的榜单范围，包含attemptsRemaining；完成、明确缺数及重试耗尽项不再列入。每次先完成全部榜单，再执行日期补查；progress会在排名结束后固化可用日期缓存并给出日期pending。还有可执行项或全局阻塞时finalize拒绝归档。只有用户明确取消，才可执行`abort RUN_ID --token TOKEN --reason "用户取消原因"`封存不完整轮次；日常任务不能用abort绕过检查。

正常采集按scanFamilies生成计划，当前仅应用共144范围；缺少开关的旧配置默认为应用与游戏共240范围。当前配置comparisonVersion=latest_valid_scope_v1，新浏览器轮次按各范围从本轮之前的归档历史逆序找最近兼容完整快照，不设回溯期限。排名规则、设备、前80门槛、时区和分类定义须一致；已归档的部分轮次中完整范围可用，正式/测试隔离。缺数和不兼容范围跳过，存储损坏停止并报错；没有兼容历史则建立该范围基线，本轮缺数不能用旧数据填充。

登记时保存baselines.json（schemaVersion、comparisonVersion、runId、scopes）及manifest.comparisonBaselinesHash。每范围记录baselineRunId、sourceDate、capturedAt、snapshotHash、skippedRuns或null。首次固化中断可恢复；摘要已保存后文件丢失/改变不得重选。当前comparisonBaselines逐触发榜单提供currentCapturedAt/currentSourceDate/elapsedSeconds，coverage.scopes.baseline保留全部范围依据。coverage新增fallbackScopes（已比较且跨过轮次）、noBaselineScopes（本轮完整但无基准）、currentMissingScopes；前两项与本轮缺数分开报告。previousRunId继续表示上一执行轮次。

旧shared_scopes_v1仍严格相邻；无版本旧轮仍整体指纹比较。缺少comparisonVersion配置时默认shared_scopes_v1。运行中变更开关会被配置指纹校验拒绝。import-legacy按完整目录导入，不裁剪旧游戏快照。

## ingest

将`pending`某scope与浏览器实际结果合并，增加真实`capturedAt`（带时区ISO8601）。新轮次sourceDate必须等于本轮固定来源日；跨天capturedAt可晚于该日但正式采集不能早于startedAt或晚于当前时间。不得将今日榜单改写日期后冒充历史榜单。scope字段为`country, categoryFamily, category, sourceCategory, genreId, chart, device, sourceUrl`。页面字段为`title, sourceDate, completeTop80, rows`。

每行字段：`position`（整数）、`name`、`qimaiUrl`、`developer`、`rating`、`ratingCount`、`latestVersionDate`；可附实际`ranks:[{rank,category}]`用于核对页面分类名次。`qimaiUrl`须为完整`https://www.qimai.cn/app/rank/appid/数字ID/country/国家`。

必须完整80行、position连续1..80、App ID不重复；程序从链接提取App ID。标题、日期、来源URL从实际页面获得，不能为通过校验填预期值。`assets/read-qimai.js`返回可直接合并的页面字段。名称遮罩如`***`照录，缺失基础字段为空。

## mark

```json
{"scope":{"country":"us","categoryFamily":"应用","genreId":"6007","chart":"free"},"status":"source_no_data","sourceDate":"2026-10-20","reason":"页面明确2026-10-20暂无榜单数据"}
```

新轮允许source_no_data、failed。source_no_data必须传固定来源日及该日明确缺数证据的原因，空白/加载中不算；跨天源站明确不再提供本轮来源日也可记缺失。failed必须提供唯一attemptId（1～100个字母、数字、下划线或连字符），每次真实失败用新ID，重复保存同一次失败使用原ID，不重复计数。初次加maxScopeRetries次重试，次数保存在manifest中，恢复不清零。耗尽后不再执行该项；成功后旧原因清除。成功范围不可覆盖，重发相同快照幂等接受。

旧生命周期仍接受auth_required、interrupted；新轮全局登录/工具中断必须suspend，不能mark全部范围再强行归档。

例如单项失败：`{"scope":{"country":"us","categoryFamily":"应用","genreId":"6007","chart":"free"},"status":"failed","attemptId":"scope-6007-free-1","reason":"该范围DOM解析失败"}`。

## 首次发布日期补查（新规则轮次）

```sh
python3 scripts/monitor.py prepare-release RUN_ID --token TOKEN
python3 scripts/monitor.py ingest-release RUN_ID --token TOKEN --input data/release.json
```

新轮没有总时限；跨天排名来源日及上线准入参考日均不改变，日期核实继续。新轮次manifest保存独立`releasePolicy`、北京时间参考日和1～6个月起始日期；排名比较版本及指纹不因上线筛选改变。旧轮次和legacy_import保留原规则，不能调用日期补查接口。

`prepare-release`在排名候选中按国家＋App ID去重，将已核实缓存固化到本轮`releases.json`，返回`pending`（country/appId/name/sourceUrl）、`candidateApps`和`verifiedApps`。允许重复调用；无候选则pending为空，不额外访问网页。

`ingest-release`省略`--input`时读标准输入。以下仅为接口格式示例，日期、文本和时间必须使用实际页面证据与实际核实时间：

```json
{"country":"us","appId":"123","pageAppId":"123","pageCountry":"美国","status":"verified","releaseDate":"2026-10-01","sourceUrl":"https://www.qimai.cn/app/rank/appid/123/country/us","sourceLabel":"最早发布","rawText":"最早发布 2026-10-01","observedAt":"2026-10-20T04:10:00Z"}
```

verified必须校验可见App ID、国家、明确的最早发布标签及与原文一致的合法日期。状态也支持unavailable、failed，此时releaseDate为空并必须有reason。晚于本轮参考日的日期会转为待核实，不能作为近期发布入选。来源URL须与候选App ID、国家一致；新轮核实时间须不早于startedAt，正式核实不得晚于当前时间；不限制为90分钟或同一天。缓存保留原核实时间。相同证据幂等接受；同轮已核实证据不能静默覆盖，结束后不可写入。

新轮finalize仅在没有可执行项时允许；日期不完整时将新规则轮次记为partial；排名覆盖数不因此减少。新报告counts增加rankCandidates、excludedOlder、pendingRelease；entries只含近6个自然月的已核实候选，releasePending单列未知记录。所有候选都未核实时confirmedNew为null，零排名候选且有可比较范围时为0。重建仅使用每轮固化证据，不用后来缓存回写历史。

报告展示层将无排名比较依据的数量显示为0，并保留覆盖与待比较说明；report-data.json中的null不改写为0。已有排名候选但日期全部未知时，展示层仍显示待核实。

日期补查次数保存在本轮记录中。prepare-release的pending项包含attempts和attemptsRemaining；首次尝试加最多2次重试，累计用尽后不再返回到pending，exhaustedApps说明仍未核实的耗尽数量。重置或resume不清零；新轮持久化全部尝试的证据摘要，A、B保存后再重发A也不增加次数或覆盖最新证据。新轮prepare-release/ingest-release不受90分钟限制。无executionPolicy的旧轮次仍保留deadlineAt/windowExpired及旧窗口校验，重建历史不切换策略；旧版导入不启用新执行策略。

## 历史报告保护

render、finalize后报告生成及邮件预览复用已归档report-data.json，不重算已有历史结果。已有report.md/report.html不重写；日期HTML和Markdown均检查实际包含的归档轮次集合；新增、缺失或中断未补齐时才更新，混合日期页读取旧轮冻结结果。缺失HTML/Markdown可从冻结JSON重建；历史JSON缺失但已有报告时明确报错、恢复原文件，不能自动重算。新封存尚无任何报告的轮次允许首次计算。旧邮件content.json与发送记录继续冻结，render不发信。

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
