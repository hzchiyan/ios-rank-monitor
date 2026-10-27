# 本机内置浏览器采集

## 连接和登录

读目标工具文档，用实际入口选择内置浏览器（当前Codex通常提供`cua_repl`）。不依赖个人技能、CDP端口或旧标签页编号。先start登记或领取轮次；缺少浏览器控制能力时suspend记录阻塞原因，保留进度等待恢复，不封存。

打开第一个计划URL，观察登录及榜单状态。优先复用会话。需要密码登录时，核实为`https://www.qimai.cn`，按可见页面进入表单，直接读取`.local/credentials.json`填写，不输出凭据。未配置时由用户在内置浏览器登录。

遇验证码、短信、二维码、风控或额外协议，记录所需人工动作，不反复尝试或绕过。页面已登录不能证明自动密码登录可用。首次身份选择使用本项目已确定的“产品研发”；涉及新协议/授权服从目标工具的权限要求。

## 每个scope

当前正常计划只有应用分类，共144范围；严格使用pending，不额外遍历完整目录中的游戏分类。以下同名分类与游戏标签说明仅用于识别历史证据和旧版导入。

1. 使用`pending`返回的sourceUrl导航。路径为`/rank/index/brand/<free|grossing>/device/iphone/country/<us|de|jp>/genre/<编号>`，实际编号来自配置，不猜私有接口。
2. 等内容出现，确认标题和筛选为目标国家、分类、榜单、iPhone、本轮固定sourceDate。`#date-box`显示实际选定日期。跨天续跑通过公开可见日期控件选择本轮来源日，检查真实日期后才读取，不猜日期URL或私有接口。源站明确不再提供该日榜单时记录该日缺失及可见原因；无法确认时保留失败或阻塞，不能改用今日榜单。旧页面需刷新/导航；URL正确不代表DOM已经切换。
3. 等至少一条App，再正常滚动/End触发懒加载。初次通常50行，至少80行再读取。每次操作后观察新状态，不用长睡眠猜测。
4. 明确所请求来源日无数据/不再提供该日榜单才mark source_no_data，并传本轮sourceDate及可见原因。空白、加载、登录遮挡、超时均不算。单项failed每次真实尝试用新的attemptId，重复保存使用原ID；初次加最多2次重试。全局登录/工具/网络失败suspend，不批量mark，不耗尽所有范围来提前结束。
5. 阅读仓库`assets/read-qimai.js`，通过文档允许的只读DOM evaluate接口执行表达式文本。它返回真实document.URL、标题、日期、行数、前80行，只读DOM，不访问隐藏状态或网络。合并时实际页面字段必须覆盖计划字段，尤其不能用计划URL替代真实URL；同名的应用/游戏分类靠genreId路径区分。
6. 在工具编程环境合并当前scope、页面真实结果与当前UTC采集时间，立即写入data并调用ingest。只有程序接受才算成功。

## 已观察字段

App行：`tbody tr`中含`.app-name`的行。名次：首个`td .index`。名称/链接：`.app-name`；开发者：`.company`；评分：`.comment-rating`和`.comment-num`；版本日期：`.app-version`。各层名次：`.rank-wrap`中`.num`和`.category`，最后一层匹配目标分类和position。日期：`#date-box`；标题：`document.title`。

应用与游戏同名分类按大类和genreId区分。“娱乐场游戏”的页面分类标签为“娱乐场”，配置已映射。父级标签偶有源站异常；最终分类名次与目标匹配时保留原来源，不自行改分类。付费下载量列不在需求内。

DOM改变则记录解析失败；日常任务不自行改解析代码。维护请求更新程序和测试后再恢复。

## 调用提示

取得并观察tab后，在工具编程环境读取`assets/read-qimai.js`，将完整表达式文本传给文档规定的只读evaluate。文件已经包含自执行括号，不需要再次包装。使用文件系统仅为读本地适配函数、保存结果，不代替浏览器联网。优先返回结构化对象，避免一个大outerHTML字符串被截断。不要把截断结果标成功。

工具重置后重新获取句柄，用领取的令牌resume同run，不依赖旧变量。新轮次没有90分钟硬截止；执行权租约15分钟，每次保存、resume、progress续约，长操作前后及时续约；失去令牌所有权应停止，不能读取新令牌抢占。每个scope保留实际capturedAt，跨天允许晚于来源日，但sourceDate必须固定。progress的canFinalize=true才finalize，已成功快照不重复覆盖；仍有可执行项继续，整体受阻suspend。恢复旧轮完成后等下一正常时点开新轮。展示本地报告和覆盖，不以样本通过宣称全量完成。

## 新进候选的最早发布日期

全部榜单范围处理结束（已保存、明确缺数或重试耗尽）后调用`prepare-release RUN_ID --token TOKEN`，仅访问其pending清单；按国家＋App ID去重，不能为全部榜单App重复访问。已核实缓存由程序固化到本轮，不伪装成本轮新采集。

1. 导航pending中的sourceUrl（应用详情页），等待可见`.app-header .i-id .new-value`显示当前App ID，再核对头部所选国家。URL正确不代表头部已经切换，二者都须一致。
2. 通过文档允许的只读evaluate执行`assets/read-qimai-release.js`完整表达式。当前适配依据：`.app-header .new-item`中`.type`文字恰为“最早发布”，日期取该项`.value`；App ID取`.i-id .new-value`，国家取头部可见的`.ivu-dropdown-rel .icon-flag`所属文字。只读可见DOM，不读框架状态或私有接口。
3. 在工具编程环境合并pending项、实际页面结果和真实当前UTC `observedAt`，实际页面字段覆盖计划字段，立即`ingest-release`。读到有效日期才用verified；标签缺失/日期不可识别用unavailable并写原因，单项导航失败用failed；整体登录/网络失败suspend。不能退回“最近更新”“版本日期”或猜测日期。
4. 初次加最多2次重试，次数由本轮证据持久化；用尽后保留待核实，不能冒充已核实或已排除。工具/登录整体失败suspend等待恢复，不封存；新轮次completion_v1的日期补查可跨天继续，没有90分钟截止；历史无executionPolicy仍按旧生命周期。
5. 同轮恢复用resume、prepare-release重新取得待核实清单。结果由程序按本轮固定参考日判断；六个月以外排除，晚于参考日或未知单列。不要让日期过滤删掉原始前80，也不要为旧轮次补查。
