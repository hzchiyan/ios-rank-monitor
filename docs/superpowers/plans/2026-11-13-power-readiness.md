# 接电运行环境保障实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将接电保持系统唤醒、允许锁屏和显示器关闭的要求变为仓库策略、可复用部署命令及启动检查。

**Architecture:** 独立config/runtime.json不进入排名比较指纹。新增标准库runtime模块及environment-check/environment-apply命令；正常启动只读检查，只有显式部署命令改变接电电源参数。持久系统策略覆盖调度前及整个浏览器采集期，不以短命CLI保活替代。

**Tech Stack:** Python3.11+标准库、macOS pmset/osascript；不安装依赖。

**Spec:** 用户本会话确认：接受接电保持系统唤醒，只锁屏及关闭显示器，并同步仓库设置。

## Global Constraints

- 应用/游戏任务保持暂停及原时点、项目、模型；不采集、不发信、不自动提交推送。
- 策略仅macOS：requireACPower=true、acSleepMinutes=0、displaySleepMinutes=10；电池策略不改。
- 不取消锁屏密码、不自动解锁、不承诺合盖/手动睡眠/注销/关机后照常运行。
- 系统修改仅显式environment-apply；管理员认证由原生系统完成，密码不进入程序或聊天。
- 缺策略的旧副本保留兼容；存在但损坏则失败，未采集状态不冒充完成。

## Review Focus

- AC/电池/无法读取及无接电配置不能误判ready。
- 环境不满足须在登记轮次和令牌前阻断，包括旧更新器激活新代码后的start。
- 只修改接电sleep/displaysleep，保留电池、锁屏、安全设置及任务状态。
- 管理员拒绝、命令失败、读回不一致必须明确，先保存可恢复的原参数。
- 候选校验/测试不修改真实系统；新策略不破坏旧配置及历史结果。

### Task 1: 策略及系统命令

Files: create config/runtime.json, scripts/rank_monitor/runtime.py, tests/test_runtime.py; modify scripts/monitor.py.
Interfaces: check_environment(root)->JSON；apply_environment(root,authorize=False)->JSON。检查输出ready/status/checks和脱敏原因；apply先本地保存原AC参数，再运行固定pmset参数，读回核对。部署参数仅接受接电sleep=0、显示器1～180分钟，缺失策略not_configured兼容，非法策略blocked。

- [x] 写输入解析、配置非法、AC/电池/未知、电源项缺失、只读无副作用、部署拒绝/成功/读回失败测试，观察RED。
- [x] 实现模块与CLI；environment-check不改系统，environment-apply默认不交互，--authorize允许本机管理员系统认证，日志只在data/environment。
- [x] 目标测试GREEN。

### Task 2: 启动衔接及部署

Files: modify scripts/update_start.py, scripts/monitor.py, tests/test_updates.py, tests/test_runtime.py; docs/README/AGENTS/skill/references/task templates.
Interfaces: 正式browser start在登记前重新检查；updater激活后以新进程检查策略并保存environment结果。test/import/render/mail不新增系统副作用。更新集成测试使用临时pmset替身，不使用真实电源设置；旧副本无策略仍兼容。

- [x] 补启动阻断、无登记副作用、旧副本兼容及同步后新策略生效测试，观察RED。
- [x] 实现调用与新任务描述，保留同步/比较/邮件/恢复口径。
- [x] 运行全量python3 -m unittest discover -s tests -v并审查。
- [x] 一次独立最终审查，重要问题统一RED→GREEN修复后全量复测。
- [x] 本机environment-apply应用接电策略并读回，若需要认证弹窗由用户本机输入；不改电池或锁屏设置。保留任务暂停，核对历史摘要和隐私。
