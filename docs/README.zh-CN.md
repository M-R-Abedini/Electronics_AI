# Electronics AI — 1.0.0

[English](../README.md) · [فارسی](README.fa.md) · [简体中文](README.zh-CN.md) · [العربية](README.ar.md)

Electronics AI 是用于 Altium Designer 原理图和 PCB 设计自动化的本地插件，
包含一个 `mcp-electronics-ai` 连接和环境设置、电路设计、PCB 布局三个技能。
[Electronics_AI 仓库](https://github.com/M-R-Abedini/Electronics_AI)是源码来源；
已安装缓存和 ZIP 均为该仓库的部署产物。

**要求：Windows、Python 3.12 和单独安装并授权的 Altium Designer。**
现有原生实测使用 Altium Designer 26，并不证明其他版本已通过验证。

## 功能

- 读取和编辑原理图、PCB，以及 `.SchLib` / `.PcbLib` 库文件。
- 根据电路功能、真实焊盘几何和项目约束比较布局方案；使用已存储的明确移动
  计划执行修改，并检查数据新鲜度和原生回读结果。
- 通过 `si_pi__` 检查实际二维铜几何、分类网络、预览规则和定位违规。
  缺失叠层或物理证据时保留未知结果，不宣称通过。
- 搜索、暂存云库并记录来源和哈希；封装及引脚到焊盘的映射仍需单独核验。
- 保存磁盘文件检查点，共享原生命令队列并记录操作结果。

专用规则应按各功能模块及本项目数据选择；某块示例板的数值和布局不能直接
作为所有设计的固定模板。

## 安装

在已准备好的开发环境中构建并安装：

```powershell
.\.venv\Scripts\python.exe scripts/build_plugin.py --smoke --check-reproducible
codex plugin marketplace add .\dist\marketplace --json
$installedPlugin = codex plugin add mcp-electronics-ai@electronics-ai --json | ConvertFrom-Json
$pluginRoot = $installedPlugin.installedPath
py -3.12 "$pluginRoot\scripts\plugin_runtime.py" bootstrap --altium-exe "C:\Program Files\Altium\AD26\X2.EXE" --library-dir "D:\MyProject\Libraries"
py -3.12 "$pluginRoot\scripts\plugin_runtime.py" doctor --probe-backends
```

替换为实际的 Altium 和库目录。`bootstrap` 新建独立的私有 Python 环境，
以普通 wheel 安装 EDA。配置保存在缓存之外，默认路径为
`~/.codex/plugins/data/mcp-electronics-ai`；marketplace 名称为 `electronics-ai`。
安装或更新后重新连接 Codex。日常启动不会安装依赖。

当前设置命令为 `bootstrap`、`doctor`、`configure-clients` 和 `serve`。
详见[打包、安装与验证](PLUGIN_PACKAGE.md)和[Windows 安装器及其他客户端](INSTALLER.md)。

## 验证范围

安装成功或列出工具并不证明 Altium 原生连接可用，需另行进行只读连接检查。
布局通过也不证明布线完成、DRC 无误或可以投产。批量编辑失败可能留下部分
修改；磁盘文件检查点无法恢复所有未保存的缓冲区状态。结果不明确时先检查，
不要直接重放操作。

参见 [PCB 布局技能](../skills/electronics-ai-pcb-layout/SKILL.md)、
[铜几何 SI/PI](COPPER_SI_PI.md)、[编辑保护](PRODUCT_SAFETY.md)和
[许可证说明](../THIRD_PARTY_NOTICES.md)。
