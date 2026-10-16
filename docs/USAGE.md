# 使用与退出状态

普通流程：import→create全新single任务→明确费用后run→show/核原文→budget。创建不代表执行。可用sources/list查看资料和任务，cancel请求停止。完整命令以deeper --help及各子命令--help为准。

low最多1次调用，仅直接草稿；medium最多4次，通常只能规划/生成/评审/核验；high最多10次，可在剩余额度允许时修改。规划、纠正和失败全部计入。后续步骤没有足够调用闭合时会以closure_budget停止；不通过跳过核验来报告成功。

传输结果未知时保留预留并停止。resume TASK_ID优先重放已保存的本地处理；只有明确允许再次计费时才用resume TASK_ID --retry。未知旧请求可能已经计费，不能因为超时自动重发。旧版本任务不要直接恢复。

## 实验接口

显式--mode multi计划先3条、最多5条候选，每条使用原high流程；错误或预算可导致未完成。默认共同0.30美元、60次调用、640k估算tokens、60分钟；子high仍最多10次/160k/12分钟。候选合计最多用共同费用80%，预留其余给共享规划/比较。只操作父任务，查看search_complete，不把partial或所选答案通过称作完整外层完成。multi收益未证明。

--mode single-budgeted给单条原loop同样共同封顶，用于策略研究，不强制花完费用。研究接口不是普通single的成熟度承诺。

评估接口deeper eval run需要你自己的manifest：用--manifest明确指定。公开仓库不附本地论文、实验题库、rubric、账本或评分结果；默认evals/manifest.json不是发布包的一部分。所有执行仍由原程序记账，评估也可能计费。eval export仅导出已有数据，会包含资料/答案，导出结果不要直接放进公开仓库。给评分者匿名答案和证据，映射另外保管。
