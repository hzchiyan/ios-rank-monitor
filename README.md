# iOS 榜单监测

可克隆到其他Mac的Codex项目技能。Codex用本机内置浏览器读取七麦网页，Python工具校验比较，生成只保存在本机的Markdown与可筛选HTML报告。

## 固定口径

| 项目 | 规则 |
|---|---|
| 范围 | 美国、德国、日本；iPhone；24个应用分类＋16个游戏分类 |
| 榜单 | 免费榜、畅销榜，各完整前80，共240个范围 |
| 时间 | 北京时间每天02:00、06:00、12:00、18:00 |
| 入选 | 免费榜新进前80 **或** 畅销榜新进前80 |
| 比较 | 上一次正式任务的同国家、同大类、同分类、同榜单 |
| 去重 | 同国家、同分类、同App合并触发；跨分类、国家、时点分别保留 |
| 缺数 | 待比较，不计为零，不回退更早成功快照 |

首次建立基线。前次不在前80，不能推断具体旧名次。本项目不输出实际下载量或下载量估算。

## 新电脑开始

1. 准备macOS、Git、Python3.11+、已登录的Codex桌面应用和内置浏览器控制能力。
2. 克隆仓库，在Codex中作为本地项目打开，不需要创建者的聊天记录。
3. 在新会话输入：**请按本仓库部署说明初始化并运行监测。**
4. 按[部署指南](docs/deployment.md)检查环境、核实浏览器与账号，试跑后配置目标机任务。

```sh
python3 scripts/monitor.py doctor
python3 -m unittest discover -s tests -v
```

程序检查不能证明浏览器、账号或定时任务可用。[项目技能](.agents/skills/ios-rank-monitor/SKILL.md)和[任务描述](docs/automation-prompt.md)均随仓库保存。

## 本地报告

```text
data/
  state.json                         # 正式任务顺序和相对路径
  index.md / index.html              # 日期总览
  YYYY-MM-DD/
    index.md / index.html            # 当日时点和离线筛选
    runs/<runId>/
      manifest.json                  # 状态、版本、比较轮次
      snapshots.jsonl.gz             # 一份压缩快照
      report-data.json / report.md
      report.html                    # 打开该时点的日期筛选页
      checkpoints/                   # 运行中分片，归档后清理
  test/                              # 测试历史，不影响正式基线
.local/
  credentials.json                   # 可选本地凭据，权限600
  tokens/<runId>                     # 本轮操作令牌
```

完成一轮后，用Finder打开`data/index.html`，或用Markdown阅读器打开`data/index.md`。HTML支持日期、时点、国家、大类、分类、榜单与关键词筛选，切换新进App和完整榜单。完整榜单每次看一个时点、一种榜单。

HTML内嵌所需数据，无外网字体、CDN或本地JSON请求，无需服务器。单次HTML入口依赖同日期页，复制历史应复制整个`data`。

GitHub仅保存程序、技能、说明和合成测试；`data/`、`.local/`被忽略，不自动推送。不要强制添加忽略文件。存储随四次全量采集增长，不自动删除历史。

## 能力边界

网页读取成功不等于自动登录通过。浏览器能力、账号权限、验证码、网页变化、目标机调度需要实际验证。短信、扫码、验证码或风控受阻时保留失败记录，要求人工恢复，不绕过验证、不自动购买会员。

采集是一个时间窗口，各范围保存实际时间。每范围最多重试2次，单轮窗口90分钟；错过时点不补造数据。同次可恢复进度，下一时点新建一轮。电脑保持运行、联网，桌面应用保持打开。

本版本不包含飞书、微信发送或网站托管。迁移验收方法见[部署指南](docs/deployment.md)。
