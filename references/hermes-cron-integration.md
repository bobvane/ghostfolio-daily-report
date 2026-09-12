# Hermes Cron 集成（对接说明）

最后更新：v10.3（2026-09-13）

> ⚠️ **本文不是第二份执行步骤。** 每日流程、字段含义、闸门判据一律以
> `SKILL.md` 的「执行步骤」+「1.5 数据质量闸门」为唯一来源。
> 这里只写 **SKILL.md 不覆盖的部分**：Cron 任务的接线方式与推送闸门的消费契约。
>
> 为什么加这句：本文件长期停留在 v5.1 —— 里面还写着 `ghostfolio_report_template.j2` +
> jinja2 渲染、把三位投资人当一等产出，而这两样早已不存在。同一套流程留两份文档、
> 其中一份不跟着改，就是评审 Grok 指出的漂移源（agent 读错一份就按旧流程跑）。

---

## 核心原则

**Skill 不调用任何外部 LLM API。** 脚本只输出**结构化 Prompt**，
由 Hermes Cron 的当前主模型处理（`model: null` 表示跟随用户当前选的主模型，不固定）。

---

## 实际接线

任务定义在 `/opt/data/cron/jobs.json`，`name = "Ghostfolio 每日持仓诊断"`：

| 项 | 值 |
|---|---|
| 触发 | `0 21 * * *`（每天 21:00 CST，美股收盘后） |
| 装载 skill | `ghostfolio-daily-report` + `chinese-user-output` |
| `model` / `provider` | `null` —— **跟随用户当前主模型**，不固定 |
| 类型 | agent 任务（`no_agent: false`）：prompt 驱动，脚本退出码由 agent 消费，不由调度器消费 |
| 推送 | `hermes send --to feishu --subject "Ghostfolio 每日持仓诊断" --file /tmp/gf_report.md`（唯一渠道） |

**agent 任务的关键含义**：`ghostfolio_collect.py` 没有 `sys.exit`，
即使 `data_quality` 全 fail 退出码也是 0。所以**退出码不构成闸门** ——
闸门只能是 prompt 与 SKILL 里写明的字段判据（见下）。

---

## 推送闸门：必须消费 `severity` / `failure_codes`

判据与三档处理写在 `SKILL.md` §1.5，**推送前必须先读这两个字段**，
而不是只看 `/tmp/gf_report.md` 有没有生成：

```
severity == "ok"        → 正常推送
severity == "degraded"  → 推送 + 标题带「数据不完整」
severity == "critical"  → 推送 + 标题带「数据不可用」+ 显式告警一行
```

- `exit_hint` 与 `severity` **恒等同档**（契约测试断言）—— 调度侧读哪个都一样。
- `failure_codes` 说明坏在哪：`PORTFOLIO_INCOMPLETE` / `MARKET_DATA_MISSING` → 立即告警；
  `SOURCE_FAILED` / `FUNDAMENTALS_DEGRADED` / `ML_UNAVAILABLE` → 记一笔、报告照推。
- **无论哪一档都要推送** —— Bob 明确选择「收到一份说明了自己缺什么的报告」，
  而不是「什么都收不到」。缺数据不许说成正常，也不许静默跳过。

---

## 产物溯源（判断「这份报告是哪套代码产出的」）

`/tmp/gf_report.json` 的 `provenance` 块：

```json
{"schema_version": "10.3", "config_version": "10.3",
 "code_version": "10.3", "code_fingerprint": "…", "generated_at": "…"}
```

`code_fingerprint` 是 5 个流水线脚本（collect / rules / gen_report / news / market_open）
的内容哈希前 12 位。生产目录不是 git 仓库，指纹比 commit 更适合回答这个问题，
并且能抓到「collect 升级了、gen_report 没升级」这类半升级状态。

---

## 消息面翻译（agent 负责，脚本不做）

脚本只负责**抓取与落盘**（`cache/news/`）—— **不要再 web_search 搜新闻**。
翻译由 agent 完成：读 `llm_analyst_prompts.news_translation_task`，把英文标题逐条译成
一句中文（≤40 字，直说要点、不加评论），按原顺序写回：

```json
{"news_translations": {"per_symbol": {"QQQ": ["译1", "译2"]}, "macro": ["译1"]}}
```

数组长度必须与给定标题数**一致**（错位会把译文挂到别人的标题上，且看起来完全正常）。
没新闻的标的不写键。`gen_report.py` 读不到译文时标注「（标题未翻译，本条不纳入结论）」
并**不纳入结论** —— 不会静默回退英文（「全篇中文」是硬要求）。

---

## 推送后的收尾

- 只回报**一句话**结论（推到哪个渠道、有无异常），不复述整份报告。
- 报告正文一律以 `gen_report.py` 的输出为准，**不要自写格式**。
