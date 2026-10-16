# 开发与本地发布准备

```sh
PYTHONPATH=src:tests .venv/bin/python -W error::ResourceWarning -m unittest discover -s tests
python3 scripts/prepare_release.py --check
python3 scripts/prepare_release.py --output .work/public-candidate
```

全部测试是临时数据和虚拟服务；不需要真实API key。少量测试启动本机回环HTTP服务，POSIX测试包含真实伪终端。不要在你的真实配置目录用交互向导作自动化测试。

公开文件必须显式加入release-files.json，同时同步.gitignore与MANIFEST.in的精确允许条目。新文件默认不公开；不要把用户资料或真实模型响应当作测试fixture。原始实验规约/结果保持本地，通用测试fixture只用合成或可公开的小例子。

prepare_release.py只读列出的文本文件，先完成检查再创建全新候选目录；拒绝已有输出目录。报告含公开文件的SHA256，不含扫描命中内容。该脚本不运行Git、不发布、不构建包，也不调用模型。

构建需在干净候选目录中进行，避免旧egg-info/SOURCES清单携带本地文件。wheel只应有程序/defaults及分发元数据；sdist额外包含许可证、公开文档/示例/测试。两种归档都要解包列出文件并再次扫描，不能只检查.gitignore。未获明确发布授权前，不创建远端或推送。

MIT许可证见LICENSE；第三方导入材料不在发行包内。功能/API入口变动时跑相称回归；不要通过改评分或省略核验范围声称研究质量提升。
