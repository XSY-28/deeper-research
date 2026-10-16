# 配置与密钥

在你选择的工作目录使用 `deeper init`。默认路径为当前目录的config.json；自定义可用：

```sh
.venv/bin/deeper --config /path/to/settings.json init --interactive
```

向导先选择已有profile，再逐项填写模型名称、API根地址、输入费率和输出费率，最后隐藏输入API key并确认保存。每项回车保留原值；重新运行同一向导即可更换这些设置，不需要手动编辑JSON。向导不请求模型，也不联网校验模型是否存在或密钥是否有效。

选择deepseek、openai_chat或openai_responses决定使用哪种接口协议。API地址填写根地址，例如https://api.example.test/v1，不包含具体的/chat/completions或/responses端点。地址需为HTTPS，本地回环可用HTTP；不接受内嵌账号、查询参数或片段。费率单位为USD/100万tokens，输入和输出分别填写有限、非负数值，按所用服务的实际价格核对。非法输入会在当前项重新询问，不会直接保存。

修改只更新选中的profile并设为默认，其他profile、选项与工作区预算保留。确认前取消或中断不写入；保存摘要显示模型、地址及两项费率，方便核对。新建任务使用新配置，已有任务保留创建时的模型与费率；可用fork OLD_TASK_ID --profile PROFILE_NAME创建使用新配置的任务。

key在配置JSON旁的.env，环境变量优先；不要将key写入profile、问题或资料。支持手工设置DEEPSEEK_API_KEY或OPENAI_API_KEY，变量名由profile的api_key_env指定。

首次终端交互、显式--interactive、脚本--no-interactive均可用。不能隐藏输入的终端会拒绝输入，不降级为回显。已有值默认保留；替换需确认；回车可跳过。写入key的文件权限0600，拒绝符号链接key文件。向导不会修改系统环境变量。

保存确认前取消不落盘；确认后保存失败会报告可能部分保存和恢复命令。key先保存、公开配置后提交，不能将异常退出当成功。使用同一--config重新运行init --interactive检查即可。没有多文件事务保证，也不校验真实账号权限或余额。

公开配置示例为config.example.json。高级接口选项仍在配置文件中维护；向导保留这些选项，不自动推断新模型支持的参数。可选协议为deepseek_chat/openai_chat/openai_responses。它们是适配器能力，示例模型名和价格不构成服务当前有效性承诺。自定义提供商先核API协议及费率，实际请求前保持TLS验证。

每个任务冻结profile和实现版本；改默认配置不改变旧任务。跨实现版本继续会被拒绝，应明确创建新任务，不改旧SQLite记录。重新初始化已有工作区不会重置预算或清除未知预留。
