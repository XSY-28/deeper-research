# Deeper Research

面向论文、笔记和技术文档的研究 CLI，内置来源管理、评审修订和预算控制。

导入几篇论文或几份笔记，写下一个具体问题，再设好预算。Deeper Research 会查找资料、起草带引用的回答，并在剩余额度允许时评审和修改。你可以回看候选答案、检查意见和每次调用的费用。

## 特性

相比直接向通用 Agent 提交研究提示词，Deeper Research 将研究步骤和执行约束交给程序管理：

- **固定资料，保留出处。** 导入时保存原文件与提取文本，任务绑定来源。原文件更新后仍保留旧副本，方便复核当时的引用。
- **内置评审和修订。** 创建任务时固定问题与验收要求，后续评审围绕这些要求展开，在剩余额度内修改草稿，无需手动串接每一步。
- **预算由程序执行。** 每次调用前检查任务与工作区余额，规划、失败和纠正请求统一记账。结果未知时保留费用预留并停止，避免自动重发。
- **研究过程自动留档。** 保存候选答案、检查意见、调用记录和结束原因，支持回看修改过程，并区分已达标、部分完成和未核验的结果。


## 开始使用

需要 **Python 3.11+**，面向 macOS / Linux。下载源码后，在项目目录安装：

```sh
uv venv .venv --python python3
uv pip install --python .venv/bin/python .
.venv/bin/deeper init
```

这里使用 [uv](https://docs.astral.sh/uv/)。也可以用 Python 自带的工具：

<details>
<summary>使用 venv 和 pip 安装</summary>

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/deeper init
```

</details>

第一次在终端运行 `init`，会引导你选择接口配置，填写模型名称、API 根地址和输入／输出费率，再输入 API key。每项回车保留当前值，费率单位为 USD / 100 万 tokens；保存前会显示设置摘要。

密钥输入不回显，单独保存在本地 `.env` 中，文件权限为 `0600`。向导不请求模型，也不产生模型费用。

以后更换模型、服务地址、费率或密钥，重新运行同一向导即可：

```sh
.venv/bin/deeper init --interactive
```

自定义模型、环境变量和非交互初始化见[配置说明](docs/CONFIGURATION.md)。使用前请核对所选服务的模型名和费率。

## 使用示例

仓库附了一份简短的[示例材料](examples/source.md)，可以先用它熟悉流程。你自己的资料可以是带文本层的 PDF、Markdown 或 TXT。

**1. 导入资料**

```sh
.venv/bin/deeper import examples/source.md --title "Example notes"
```

命令会返回 `source_id`。把它填到下面的 `SOURCE_ID` 处，创建一个任务：

**2. 写下问题、验收要求和预算**

```sh
.venv/bin/deeper create --question "示例材料说明了哪些使用限制？" \
  --criterion "结论能定位到资料，区分事实与推断" \
  --source SOURCE_ID --mode single --effort high --task-budget 0.04
```

`create` 只保存任务，不调用模型。这里给任务设了 **USD 0.04 的上限**；预算耗尽时会停止，可能留下未完成的草稿。

**3. 执行并查看结果**

用创建返回的 `task_id` 替换 `NEW_TASK_ID`：

```sh
.venv/bin/deeper run NEW_TASK_ID
.venv/bin/deeper show NEW_TASK_ID
.venv/bin/deeper budget
```

**从 `run` 开始会请求模型，可能产生费用。** 问题、候选答案和所需资料片段会发送给你配置的模型服务。

## 输出文件

默认保存在本地工作区：

```text
workspace/tasks/NEW_TASK_ID/
├── artifacts/              # 候选答案、检查意见等过程记录
└── exports/
    ├── final.md            # 导出的答案
    └── run-summary.json    # 完成情况与费用摘要
```

查看结果时，留意 `quality` 和 `reason`：`finished` 只表示执行结束，答案仍可能是 `partial` 或 `unchecked`。即使达到内部的 `criteria_met`，也需要核对引用是否真的支持结论。

## 预算与费用

`--task-budget` 控制单个任务，工作区另有累计上限，初始值为 USD 10。所有任务共用这本账，规划、失败请求和格式纠正都会计入。`deeper budget` 可以查看已计费用和剩余额度。

费用按配置费率和供应商返回的用量估算。请求超时、结果未知时会保留费用预留并停止，不自动重发；`resume` 和评估执行也可能计费。本地预算不会修改供应商账户的额度。

## 项目状态

日常尝试建议从 `single` 和小而明确的问题开始。`multi` 会生成并比较多个候选，目前是实验功能，尚未证明优于同预算的 single。

资料导入、来源快照、预算记账和配置向导已有离线测试。完整研究流程能否稳定达标、答案质量是否有持续收益，还没有充分验证。模型可能误读主语、否定或适用范围，也可能引用了正确的段落，却得出错误结论。

目前没有开放网络搜索或 OCR。复杂表格、公式、跨页标题和过长段落的处理有限。安装流程已在 macOS / Python 3.12 验证；Linux 和 Python 3.11 尚未分别实机验收。

## 文档

- [配置模型与 API key](docs/CONFIGURATION.md)
- [调用限制、恢复任务与实验模式](docs/USAGE.md)
- [已验证的范围与仍未解决的问题](docs/STATUS.md)
- [资料、密钥与隐私](docs/SECURITY.md)
- [开发、离线测试与发布检查](docs/DEVELOPMENT.md)

密钥、实际配置、账本和研究记录保留在本地，不包含在公开文件清单中。自行发布或分享项目时，请按开发文档导出干净副本。

## 许可证

[MIT](LICENSE)。导入的第三方资料不随项目发布，其权利由各自来源决定。
