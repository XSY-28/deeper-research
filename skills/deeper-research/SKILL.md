---
name: deeper-research
description: 用本地 Deeper Research 程序导入固定 PDF/Markdown/TXT 资料、执行有预算的研究、查看或继续任务，选择 effort 和已配置的模型 profile。用户要求用 deeper-research 或本地研究助手完成这些操作时使用；普通论文讲解或只讨论研究方案不启用。
---

# Deeper Research

把自然语言请求交给已有程序。由程序管理来源快照、研究循环、模型协议、任务状态和费用；在聊天中解释请求、组织输入、调用 CLI 和交付结果。不要另外调用模型 API、手写研究/评审循环或另算一套预算。

## 定位与调用

使用本 skill 目录下 `scripts/deeper.py` 的**绝对路径**作为入口。它解析实体路径以定位所属项目，切换到该项目目录，然后 `exec` 已安装的 `.venv/bin/deeper --config config.json`，保留程序退出码与中断行为。

程序根目录默认是入口实体路径的 `parents[3]`；`DEEPER_RESEARCH_ROOT` 可指向已存在的项目。目录失效时先找候选并确认，不自动创建新的项目或账本。模型配置在该根目录的 `config.json`，详细命令以入口 `--help` 和程序 `README.md` 为准。

用 argv 列表传参，不拼接用户文字到 shell。复杂、多行或以 `-` 开头的问题写入临时 UTF-8 文件，使用 `create --question-file 文件路径`；criteria 和 source/profile ID 也按独立参数传递。

## 将请求落到任务

1. 区分查看、导入、仅准备、执行、继续和换模型。仅查看/规划/“先不要运行”不调用 `run` 或 `resume`。用户明确要求执行研究才进入模型运行；无需重复询问已授权的步骤。
2. 读取 `sources`、`list`、`budget` 中本次需要的信息。只采用用户指定或确认的资料：新本地文件用 `import`，已有资料复用返回的 source ID；路径或同名来源含糊时澄清。暂不做开放网络搜索、OCR或远程解析；URL需先取得对应资料并确认版本，不能把其他搜索材料混入限定范围。
3. 将问题和核心完成要求转换为少量可检查的 `--criterion`，保留用户的量词、限制、事实/推断边界和资料范围。用户指定的 effort/profile 优先；否则使用程序默认 medium 和当前 default_profile，简要说明。不要把 high 当作更高质量的承诺。
4. 用 `create --question-file ... --criterion ... --source ... --effort ... [--profile ...]` 创建，保存其返回的 task ID。若授权执行，调用 `run TASK_ID`；创建本身不意味着已经运行。重用同一程序配置与 workspace，不用 `--workspace` 给每个请求新建目录，不重置原账本。
   用户要求多候选选优时加 `--mode multi`，默认high：先3条、最多5条，每条独立运行原loop；`--task-budget 0.30` 控制整次共同USD上限，仍受原项目账本约束。不要在聊天中自行生成候选、调用比较模型或合并答案。普通请求保持原single默认；`single-budgeted`只用于明确要求同预算单loop对照。细节见程序README的多候选入口。
5. 用 `show TASK_ID` 查看实际状态。仅准备或 ready 的任务返回任务 ID、来源、effort/profile 和预算，明确尚未执行、尚无研究结论；不要求其存在导出文件。已运行且存在导出时，读取 `workspace/tasks/TASK_ID/exports/final.md` 和 `run-summary.json`，以程序实际 ID/位置为准，说明结果、真实引用、未解决项、停止原因和记账估值，给产物链接。区分 finished、unchecked/partial/criteria_met 和独立正确性；程序结果不足时保留不足，不在聊天中另跑免费“补全循环”冒充程序结果。
   multi需另读`children`、`search_complete`和`selected_quality`：所选答案通过不等于所有候选流程/外层比较完成。查看或恢复使用父任务ID；父任务已停止时不要绕过父状态直接运行子任务。

## 继续、重试与模型选择

- 查看或使用完成的任务不会重新跑模型。继续前先 `show`，遵循程序状态；结果未知的付费调用，只有用户明确授权重试才使用 `resume TASK_ID --retry`。不要因“继续”或错误退出自动追加 retry、批量重跑或扩大额度。
- 旧任务的 profile、effort、标准和实现版本冻结。切换已配置模型或 effort 用 `fork OLD_TASK_ID --profile NAME --effort ...`，说明生成新任务并记录父任务，再按授权运行；不要改旧任务的 SQLite/JSON。
- 新 API 尚未配置时，读取程序配置格式，收集模型、API root、协议、密钥环境变量名和费率等必要信息。配置写入程序的 profile，由程序验证和适配；新协议需扩展程序适配器，skill 不增加客户端。不要读取或打印 `.env`、把密钥写进问题/skill/任务，也不要把示例配置称为真实远端兼容验证。
- 所有费用、调用/读取/token/时间限制和内部重查由程序负责。展示 `budget` 的账本余额与估算性质，额度不足就返回已有结果，不用另一个工作区或聊天模型绕开限制。
