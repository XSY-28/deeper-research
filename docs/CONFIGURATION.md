# 配置与密钥

在你选择的工作目录使用 `deeper init`。默认路径为当前目录的config.json；自定义可用：

```sh
.venv/bin/deeper --config /path/to/settings.json init --interactive
```

向导只选择已有profile与保存key，不请求模型。key在该JSON旁的.env，环境变量优先；不要将key写入profile、问题或资料。支持手工设置DEEPSEEK_API_KEY或OPENAI_API_KEY，变量名由profile的api_key_env指定。

首次终端交互、显式--interactive、脚本--no-interactive均可用。不能隐藏输入的终端会拒绝输入，不降级为回显。已有值默认保留；替换需确认；回车可跳过。写入key的文件权限0600，拒绝符号链接key文件。向导不会修改系统环境变量。

保存确认前取消不落盘；确认后保存失败会报告可能部分保存和恢复命令。key先保存、公开配置后提交，不能将异常退出当成功。使用同一--config重新运行init --interactive检查即可。没有多文件事务保证，也不校验真实账号权限或余额。

公开配置示例为config.example.json。profile需明确protocol、base_url、model、api_key_env、输入/输出费率；可选协议为deepseek_chat/openai_chat/openai_responses。它们是适配器能力，示例模型名和价格不构成服务当前有效性承诺。自定义提供商先核API协议及费率，实际请求前保持TLS验证。

每个任务冻结profile和实现版本；改默认配置不改变旧任务。跨实现版本继续会被拒绝，应明确创建新任务，不改旧SQLite记录。重新初始化已有工作区不会重置预算或清除未知预留。
