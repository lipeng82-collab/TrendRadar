# thinking tank L叔｜全文研究型选题发现
更新：2026-11-12。适用个人公开线索；不接入机构资料、Obsidian、S3 原始库，不自动写稿或发布。

## 当前可执行链路
`python -m tt_l.research`：直接发现 → 持久队列 → 网页/PDF 正文与段落证据 → 全文分块语义筛选 → 支持/反证两类检索 → 多来源全文综合 → 引用与单独核验调用 → 四核心选题卡 → 去重聚类 → 选择性通知。

- 四核心：认知、注意力、财富资产、财商教育；看懂商业/国企/第二课堂仅是场景。
- 现有 config/config.yaml 的热榜、RSS 作为原始输入；不会先走旧标题筛选。新增源及预算在 config/tt_l_research.yaml。
- 每个安全 URL 先入队，无标题打分淘汰；来源轮转调度。正文全部分块，只有全部块都判断无关才拒绝；超长、预算不足或 API 失败保留队列。
- HTML 保存来源 URL、源日期、获取时间、内容哈希、p1…段落；PDF 保存页码。支持 EC presscorner 官方 PDF 入口。日期缺失标 unknown。
- robots 不可用/禁止、付费墙、JS 壳、短摘要、扫描 PDF、超大文件均显示失败并退避重试，最长每24小时再试。不会登录、运行网页脚本或绕过限制。
- 网页内容为不可信数据；正文不能下达执行指令。抓取不使用浏览器凭据或 Jina 代理。
- SQLite 保存所有阶段及原因；每条日志包含 token/费用预留字段，真实 provider usage 另存。
- 不把全文“已提取”等同出版者全文完整认证；公示正文或研究局限均保留在 coverage 字段。

## 深研完成的含义
GitHub runner **没有继承 Codex/ChatGPT 桌面深研插件**。实现采用正式 Brave Search API（需 BRAVE_SEARCH_API_KEY）或公开 GDELT DOC API 搜索发现 + 直接原文提取 + 模型综合的替代证据流程；搜索引擎只提供链接，不作事实证据。

“深研完成”须同时满足：支持与反证搜索成功、至少两个不同发布主体有实际正文被引用、至少一个已配置官方/原始发布域名的事实引文、事实和反证段落编号存在、选题卡字段完整、单独核验调用对所有事实/反证判 supported 且提供真实存在的原文短句。两个 EC 站点视为同一发布主体。任一不满足标“深研未完成”，不推送该选题。

这证明自动证据流程走完；不能自动认证因果、数据真伪或法律结论，模型也可能错误理解真实引文。事实—机制假设—反证—应用、收益分配和风险承担分开呈现。引用原文并不授予转载全文的权利；缓存供研究，稿件另经作者审定。

## 降噪、成本与状态
默认每天（北京时间）最多3条卡；深研后价值分须达到80/100、新颖度0.65/1才进入推送队列、每轮最多3条深研；不凑数量。跨日按 URL、正文哈希、事件键抑制重复，近似标题仅记疑似重复而不单独丢弃，同一轮按事件聚类。通知沿用已有一个渠道，优先企业微信、飞书、钉钉、Slack、Telegram、ntfy；其他原有渠道保留在 legacy 手动回退里。

通知在调用前持久记录 sending，成功回执后记 sent；超时记 unknown 并停止自动重发，避免重复轰炸。unknown 需要依据渠道回执人工销号，不能假定送达。推送只发精简卡，完整卡和证据在状态库/报告。

默认每日预留上限 $2、220000 tokens、55次模型及搜索 API 调用。按 UTF-8 字节保守估算输入上限，加最大输出，调用前预留，失败不退款；这**不是实际计费单**。可选 Brave 单次查询亦按配置预留费用；默认无密钥使用公开 GDELT API，失败保留待研究，不抓取禁止访问的 Bing 搜索页面。真实 usage 存在日志，单价上限参数须不低于实际所选模型单价。保持低成本模型由现有 AI_MODEL 指定，本次未替换供应商。研究证据超上下文限制不截断伪装完成。模型缓存上限1000项。

失败项仍待处理；预算耗尽会产生积压，summary 的 queues/failures 和每日最多一次异常汇总会提示。历史去重从首次状态初始化起算，**旧版渠道历史未导入**；不能承诺覆盖此前所有已推内容。持久库不要删除或随意更换，否则去重和每日预算会重置。

## 本地运行
Python 3.12，安装 `python -m pip install -r tt_l/requirements-research.txt`，在系统环境中设置 AI_API_KEY；可选 AI_MODEL、AI_API_BASE。禁止把值写入仓库。

- 只探测来源（不调模型、不通知）：`python -m tt_l.research --probe`
- 正常研究但不推送：`python -m tt_l.research`
- 启用已有渠道通知：`python -m tt_l.research --notify`
- 只处理积压：加 `--skip-discovery`
- 16例无模型基线：`python -m tt_l.research.evaluate`
- 16例真实模型验收：`python -m tt_l.research.evaluate --live`（消耗预算；独立 eval 状态库，不发送通知）
- 全部测试：`python -m unittest discover -s tt_l -p "test_*.py" -v`

状态默认 work/tt_l_state/research.db；报告 work/tt_l_reports/{summary.json,cards.md,audit.jsonl}。运行目录不提交 Git。个人与机构必须独立目录及凭据。

## GitHub Actions
crawler.yml 改为北京时间09:17、15:17、21:17、次日03:17增量运行。取消7天到期自禁用。原 Check In 改为只读健康诊断，**不再删除运行历史或持久状态**。legacy 原程序及其 Secrets 映射保留，只有手动选 mode=legacy 才跑，避免双重推送。

可用的正式检索密钥仅放 BRAVE_SEARCH_API_KEY Secret，不输出值。手动 mode=evaluate 可使用现有 AI Secrets 跑16例真实模型指标（同一持久预算，无通知）。

首次合并后，手动 Run workflow：mode=research、bootstrap=true、notify=false，建立状态并检查模型与报告；随后可手动 notify=true 验收渠道，之后 schedule 使用通知。bootstrap 仅初始化一次；正常运行从最近同分支 tt-l-state artifact 恢复。恢复失败立即停止，避免预算与去重悄悄清零。每轮即使部分失败也 checkpoint 并上传新状态；状态快照保留7天，内部累计台账不会每7天重置；报告30天。长时间停机导致所有快照过期须从本地备份恢复后再启用，不能重复 bootstrap 冒充连续性。

分支和 PR 不改变 master 自动运行。PR CI 不使用生产 Secrets。Actions 是尽力调度，排队、配额、平台故障和公开仓库60天无活动停用仍可能发生；取消7天签到**不等于长期运行 SLA**。本次不造提交规避平台规则。严格可靠运行可把同一 CLI 放在持续在线机器的既有调度器，状态放持久盘；没有在线主机/凭据验收不能宣称该部署已完成。

## 2026-11-12 实测边界
- 已测搜索：Bing 搜索页 robots 禁止，已移除；GDELT API 请求失败；Google News robots 获取超时，不采用。Brave API 已实现但没有密钥实测，故不能声称深研检索已接通。
- 实际抓取 TikTok 2026-11-06 官方网页：10段，源日期可从 Publication 标签解析；官方完整 PDF：2页文本段，保留 page。
- 8类源入口：EC digital、BIS research/press、ECB press/publications、FTC 成功；Nature robots 获取失败，OECD 两个官方入口请求失败，未绕过。
- 16例为8正8负机制样本，含官方事件的中文转述，不是16篇真实全文语料。离线基线及传输注入测试不能当成生产模型召回率。
- 真实模型、通知回执、master定时连续性仍需线上验收；准确状态以 PR/Actions 证据为准。

## 回退
可在 config/config.yaml 关闭 tt_l_research.enabled，手动运行 mode=legacy；或在 Git 中撤销合并。保留状态库和Secrets。不要运行旧版删除全部 runs 的 Check In。tt_l/topic_cards.py 及旧人工入口保留为历史兼容工具，均不能替代自动全文链路。

独立验证：旧 clean-crawler.yml 仍启用时，可对功能分支显式 dispatch mode=ci 或 evaluate；它不会启用 crawler 定时任务，验证任务不传递任何通知凭据。evaluate预算与同分支研究状态共用，无法恢复已有状态时停止。
