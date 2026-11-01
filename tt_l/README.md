# thinking tank L 叔｜TrendRadar 选题发现（安全试运行版）

这是用户个人微信公众号的**公开新闻选题发现系统**，不是自动发稿系统。上游仍由 TrendRadar 采集新闻、RSS，并保留现有通知设置。

## 实际运行

- config/config.yaml 已使用 AI 相关性筛选；兴趣描述在 config/custom/ai/tt_l_personal.txt。
- AI 接口沿用 GitHub Actions Secrets 中配置的 AI_API_KEY / AI_MODEL（若有）。代码不新增或展示密钥。AI 不可用时回退到 config/custom/keyword/tt_l_personal.txt，由 filter.frequency_file 指定；调度时段指定的词表优先，全局原有 config/frequency_words.txt 保留不动。
- 当前只升级 **AI 兴趣筛选**。为避免意外公开可能由 S3 存储提供的原始数据，**不自动在 GitHub Actions 生成或上传候选卡**；原有 crawler.yml 工作流保持不变。
- 在已授权的本地 TrendRadar 工作区中，output/news/ 或 output/rss/ 下至少存在一个所选日期的兼容数据库，才能运行候选卡命令（示例：python tt_l/topic_cards.py --repo . --profile personal --out ../tt_l_candidates --days 2 --max 3）。缺少数据库或全部损坏会报错，与采集成功但没有合格选题的零条结果区分。
- 本地候选卡目录不要提交到 GitHub；若原始数据在 S3 中，应先核查其公开属性、来源授权、账号和存储边界，再决定是否启用专门同步。当前脚本不会主动读取 S3。
- 若当天没有符合条件的新闻，允许 0 条；不为凑数量造题。
- 个人每次最多 3 条；机构模式最多 2 条且必须在另一个已绑定的目录运行。输出目录须在仓库外；脚本拒绝内网地址、嵌入账号密码的链接和常见带凭据参数的链接。来源及事实核验仍须人工完成。
- 把最新候选卡交给 Codex，按本目录 CODEX_TASK.md 研究，才进一步核验来源、反证、读者问题和栏目适配。

## 核验顺序

1. 检查原有 GitHub Actions 工作流是否成功抓取并推送，以及 AI API Key 是否在 Secrets 中配置、费用是否可控。
2. 仅在授权的本地环境生成候选卡，核对原始链接、时间、栏目和待核验标识。
3. Codex 人工处理 3 条以内候选，作者选择是否成文。
4. 对两周结果复盘；不要用新闻热度或模型初筛分数代替真正的选题价值。

## 离线验证与回退

- Python 3.12，使用已有依赖运行：`python -m unittest discover -s tt_l -p "test_*.py" -v`。测试使用合成标题和临时 SQLite，不发送通知、不调用 AI 或 S3。
- 上线前合并此功能分支的 PR；创建分支或 PR 本身不会改变默认分支的定时运行。
- 如需退回原运行方式，可撤销本次合并提交；或先将 filter.method 改回 keyword，删除 filter.frequency_file 配置，恢复原全局词表。两项 AI 分析配置（max_news_for_analysis、include_rss）也可按 Git 历史恢复。

## 重要边界

- 个人三栏：《看懂商业》《看懂国企》《第二课堂》。
- 作者从业经历应表述为与地方政府打交道的城市规划设计、债券承销、产业与人才研究、AI 实践；不要误写成单体建筑设计。
- 湘江研究院《智库观点》必须在**另一独立项目**运行，不得把内部报告、客户资料、服务对象信息或机构账号接入本个人仓库。
- 本仓库不具备公众号发布权限；所有稿件须经作者终审。
- 当前上游 GitHub Actions 工作流含七天签到限制；本升级没有修改或绕过其检查逻辑。
