# 在新Mac部署

## 1. 只依赖仓库开始

克隆后，在Codex桌面应用中打开仓库为本地项目。使用持久化本地目录，不为每次采集新建worktree。在新会话输入：**请按本仓库部署说明初始化并运行监测。**

Codex读取AGENTS.md与本仓库技能，确认没有同名技能或指令冲突。程序要求Python3.11+标准库，无额外pip/npm运行依赖。根路径自动定位。

```sh
python3 scripts/monitor.py doctor
python3 -m unittest discover -s tests -v
```

`ready`仅表示程序和配置可用，`browserVerified:false`明确表示尚未验证浏览器。缺Python、工具或权限时报告具体缺项，不把缺能力解释为零数据。

## 2. 浏览器与账号

必须有目标桌面应用实际提供的内置浏览器控制能力，仅CLI不满足此版本采集要求。读取工具文档后正常打开七麦，验证一个分类页面。不依赖旧标签页编号、浏览器ID、CDP端口或创建者的登录状态。

建议用户在目标机内置浏览器完成首次登录。可选配置本地密码登录：

```sh
mkdir -p .local
chmod 700 .local
cp config/credentials.example.json .local/credentials.json
chmod 600 .local/credentials.json
```

用户本地编辑JSON，填写`qimai.username`、`qimai.password`，不要把密码发送到聊天。只有七麦实际提供密码登录表单时，Codex才可直接读取并填写，不能输出凭据。配置文件不保证七麦提供密码登录。短信、扫码、验证码、风控或二次验证需人工处理，不反复尝试、不绕过验证、不自动购买会员。

## 3. 手动运行与历史

先按技能做小样本浏览器测试，使用`--mode test`；这不代表144个应用范围都可用。测试数据在`data/test`，不影响正式基线。

正式采集使用实际时间，不传`--at`。首次建立基线，第二次正式任务才可比较。当前仅采集24个应用分类，共144个范围，完整任务必须逐项交代成功、源无数据或失败。旧配置没有scanFamilies时仍为240范围。当前latest_valid_scope_v1逐榜单找最近兼容且已归档的完整前80，不设回溯期限；上轮失败或缺数继续往前找，本轮缺数仍待比较。旧规则轮次保留原口径。

新运行先处理完全部榜单范围（成功、明确缺数或重试耗尽），再调用`prepare-release`，对未命中已核实缓存的新进候选，通过内置浏览器读取七麦详情页的“最早发布”，逐项`ingest-release`后归档。当前新轮仅最近12个自然月（一年）发布且日期已核实的候选进入主列表，含边界，缺失日期单列待核实。新policy版本为qimai_first_release_12_months_v1；HTML默认12、可进一步筛1～12个月。旧半年轮仍6个月准入并筛1～6，历史结果不重算；混合半年/一年或无日期规则时禁用月份筛选，保留各轮原入选结果。完整榜单不按日期删行。

要延续另一台电脑的历史，先结束旧电脑进行中的任务，再复制整个`data`（包含baselines.json及封存report-data.json），运行`python3 scripts/monitor.py render`恢复缺失页面；已有历史结果直接读取，不重算。历史JSON缺失或基准摘要变化须恢复原文件，不能重选更早基准。不要复制浏览器cookies代替正常登录；已结束历史不需要旧令牌。没有data时正常从新基线开始。

旧版研究文件可按时间从早到晚导入：

```sh
python3 scripts/monitor.py import-legacy /absolute/path/to/old-baseline.json
```

输入需有`run.runId`和完整`snapshots`数组。先校验再登记，保留原时间，来源标为`legacy_import`，不改原文件。有更新正式数据后，不能把旧记录插到末尾。

旧版导入按完整40分类目录封存240个计划范围，支持游戏快照，不受正常采集的scanFamilies限制；导入不会访问网页。报告的大类和分类选项跟随所选轮次，旧游戏数据仍可查看。

旧版导入不启用上线准入，不用当前缓存补造历史日期。正常报告重建也不回填旧轮次。发布新程序前应等待进行中的轮次结束，再同步任务描述，避免中途切换采集规则。

## 4. 定时任务

手动全量试跑后，通过Codex应用提供的调度工具或界面创建一个独立任务，选择本仓库、本地项目目录，使用[任务描述](automation-prompt.md)。不依赖创建仓库的旧聊天。

时间为 **Asia/Shanghai每天02:00、05:00、11:00、17:00**。更新已有任务时保留启停状态；暂停任务不宣称存在已启用的下一次实际运行。启用时检查界面显示的下一运行时间确为北京时间。如果界面仅支持系统时区，先解决时区设置再启用，不能假设当地02:00等于北京时间02:00。

新浏览器轮次采用completion_v1，不再因开始后90分钟结束。每次触发先start：已有执行者则skipped等待，中断/阻塞的轮次则resumed继续；恢复旧轮后不在同次触发另开新轮，等下一正常时点。progress仍有可执行项就继续，只有canFinalize=true才归档并发最终邮件。全局受阻suspend保留running，记录人工动作，不发最终邮件；仅用户明确取消才abort。

15分钟租约仅用于避免两个触发同时执行，保存、resume、progress会续约，长浏览器操作前后主动续约；无心跳后start可接管同轮并轮换令牌。租约过期不会封存数据，旧执行者失去令牌必须停止。真实时间戳保留，跨天排名必须用公开日期控件选本轮来源日；明确不再提供该日榜单才记该日缺失。发布日期核实继续，以本轮固定参考日判断。不可访问的历史日期能力需在目标机实际观察，不假设源站支持。历史无executionPolicy的轮次保留旧生命周期，不中途切换。

电脑保持开机联网、桌面应用运行。平台/会话中断仍可能发生，依赖后续触发或手动start续跑，不保证外部平台永不中断。浏览器访问和项目写入权限须在目标机实测；仓库不自动扩大权限。任务不会因clone自动创建，不直接写Codex个人目录的调度配置。

## 5. 无记忆迁移验收

- 不同路径（含空格/中文）新副本、新会话，只给入口句即可运行。
- 首轮建立基线；新轮按各范围回溯最近有效完整快照，无期限限制并标明实际跨度；旧相邻比较版本保留。
- 同版本、配置、合成快照在两处得到相同筛选结果。
- 复制data后继续读取历史，Markdown链接和HTML筛选正常。
- 在目标Mac观察一次真实定时运行，核对账号、工具、权限及文件落点。
- 用独立测试轮次模拟超过90分钟、断点恢复、全局suspend、跨天旧来源日和日期补查；有可执行项不可提前归档，接管后旧令牌不可写。

本地测试、真实网页样本、目标机定时验收分别记录，不互相替代。目标机尚不可用时，应明确列出待验收项。

## 6. QQ邮件配置与验收

邮件为可选功能。QQ邮箱需在邮箱设置中开启SMTP服务并生成授权码，使用授权码认证，不使用QQ登录密码。[腾讯官方SMTP参数](https://docs.cloudbase.net/authentication-v2/method/email-login)列出QQ的`smtp.qq.com`及SSL端口465；首版只使用SSL/TLS并校验证书。

在目标机仓库根目录准备文件，已有配置不覆盖：

```sh
mkdir -p .local
chmod 700 .local
cp -n config/mail.example.json .local/mail.json
cp -n config/mail-secrets.example.json .local/mail-secrets.json
chmod 600 .local/mail-secrets.json
```

本地编辑`mail.json`：`fromAddress`填完整发件邮箱，`recipients`填目标邮箱字符串数组，`fromName`默认“iOS 榜单监测”，`smtp`默认`host:smtp.qq.com, port:465, security:ssl`；可省略smtp字段以使用这些默认值。可选smtp.username必须与fromAddress相同。首版仅支持ASCII邮箱地址，不接受显示名称或换行。`mail-secrets.json`的`authCode`填QQ生成的授权码。两份文件均不上传；授权码不要发送到聊天，不打印文件内容。填写完成后将`enabled`设为true；禁用时无需有效账号或凭据。

```sh
python3 scripts/monitor.py mail-check
python3 scripts/monitor.py mail-preview RUN_ID
```

`mail-check`返回ready仅表示本地结构和权限通过；disabled/not_configured表示不会发信，不访问SMTP。预览保存到指定归档轮次的`mail/preview.html`和`mail/preview.txt`，先打开HTML核对全部App、七麦详情链接、首次发布区间和数量。测试轮次预览须显式加`--mode test`，不能发信。

再对用户指定的一个正式浏览器归档轮次执行`mail-send RUN_ID`验收，人工确认收件箱中的正文和链接。不为邮件验收启动采集或启用暂停任务。成功状态submitted表示服务器已接受，不保证最终进入收件箱；多个收件人分别发送，失败只补发失败项。unknown表示提交后断线或发送进程中断等结果不明，先人工核对，不删除记录或盲目重发。授权码失效后本地更新，再显式重试failed项；不能将unknown改成failed来绕过保护。

迁移历史时完整复制data会保留冻结正文和发送记录，避免旧邮件重发；迁移发件配置需另外在目标机安全配置本地授权码。更新实际定时任务描述后，必须保留当前暂停状态及其他设置。归档、渲染和导入不会自动发信。
