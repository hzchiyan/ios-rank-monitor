# 游戏定时任务设置

- 名称：七麦iOS游戏榜单监测
- 分组：games；16个游戏分类，三国双榜共96范围
- 项目：目标电脑本仓库的持久化本地项目
- 时间：Asia/Shanghai每天02:00、05:00、11:00、17:00
- 应用任务保留既有ID及设置；游戏任务初始暂停。更新时保留启停状态，不创建重复任务。

## 可直接复制的任务描述

读取仓库AGENTS.md。本任务固定profile=games，使用持久化本地项目，不新建worktree，不手动git pull/reset/stash/clean，不修改业务配置或凭据。唯一允许的代码同步是受控启动器。若.local/monitor-launcher.py尚未安装，执行python3 scripts/launcher.py install --enable-update；已有本地更新策略不会被安装覆盖。

先执行python3 .local/monitor-launcher.py startup --profile games。它在全仓库锁内检查应用/游戏及测试旧轮：有未完成轮次则延期更新；空闲才校验并快进远程main。同步失败按.local/update.json策略保留当前版本或停止，不伪称已更新。若工具返回仍运行的session/cell，按工具接口继续等待并报告阶段，不重复启动、不终止候选校验来开始采集。

读取启动JSON。update.status=blocked或没有有效run时停止业务操作，给出本组、更新状态、脱敏原因、failure中的步骤/错误、已知版本及恢复动作；registrationUncertain=true表示start结果未确认，不自造runId或另开轮次。其他结果读取run：skipped只汇报跳过；waitingForDrain表示等待两组旧轮结束，可能没有本组runId，不得另开新轮；cancelled只汇报原取消归档及按原归档规则处理，不再次start。仅本次返回tokenFile时才表示已领取执行权，可读一次该文件到本次变量，不回显或记日志；不得因后续失权重读新令牌争抢。

启动成功后重新读取本仓库最新.agents/skills/ios-rank-monitor/SKILL.md及docs/run-games.md，再按返回run执行。后续所有本地命令通过python3 .local/monitor-launcher.py command转发，显式保留--profile games和本次令牌，读取更新后的浏览器适配器，不沿用旧线程变量。startup已经完成doctor/start，不能重复登记。应用和游戏各自使用持有的标签页及数据，不串组。任何命令失败都按skill分类处理，每次可控退出给出错误、可信进度、保存状态和恢复方式，不仅留下failed调用。

完成消息先说明update状态、beforeCommit/afterCommit及本轮codeCommit（从本轮manifest读取，旧轮未记录时明确未知）；kept_current、deferred或disabled不能称为同步最新版。正常归档才按本组运行说明发送邮件并给出实际存在的本轮报告和本组总览绝对路径Markdown链接；未归档不虚构完成或报告。保留本地邮箱/授权码、数据、运行令牌、投递记录和两条任务的时点、模型、启停状态，不外发凭据。仅授权的SMTP报告允许发送。
