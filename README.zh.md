<!--
repository-topics:
  - personal-memory
  - knowledge-graph
  - chinese-nlp
  - self-hosted
  - llm
  - mcp
  - demo
-->

# Memexa · 镜我

[English](README.md) · **中文**

> 面向中文原生数据（微信 / QQ / 邮件 / 文档 / 录音）的自托管个人记忆图谱。
> **本仓库是开源 demo，完整引擎是独立的专有产品。**

[![CI](https://github.com/labazhou2024/memexa/actions/workflows/ci.yml/badge.svg)](https://github.com/labazhou2024/memexa/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/memexa?label=PyPI)](https://pypi.org/project/memexa/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](pyproject.toml)

## 这是什么

memexa 把散落、多方的中文数据组织成一张可查询的记忆图谱：每条消息**逐字**保存，抽取成结构化记忆卡，每个答案都能**引用回原句**。完全自托管——你的数据不离开本机。

本仓库提供的是**开源 demo**：一份小的合成数据集 + 一个 stub 抽取器，让你在三十秒内、无需后端、无需 API key、无需任何配置，就能看到这个项目的形态。

## 试用 demo

```bash
pip install memexa
memexa demo
```

六个合成数据源（微信 / QQ / 邮件 / 浏览器 / AI 对话 / 录音）用 stub 抽取器摄入，然后对生成的记忆卡跑五个示例查询——全程在内存中完成。这是对项目能做什么的诚实初窥。

## 完整引擎

demo 跑的是合成数据上的 stub。完整的 **memexa** 引擎是专有产品，**不**包含在本仓库中。它增加了：

- 跨多个数据源的**实时摄入**（增量）。
- 一条**双 LLM 抽取流水线**，产出带逐条引用、跨别名归一身份的记忆卡。
- 一个**多通道召回栈** + cross-encoder 精排——为杂乱、多方的中文聊天高精度检索而建，不是单向量检索。
- 一个 **MCP server + CLI**，让任意编码 agent（Claude Code / Cursor / Cline / Codex）把你的记忆当作一等工具调用。
- 一个**本地桌面应用**——整套系统跑在你自己的机器上。

如需完整引擎的使用权限，请通过仓库所有者的主页联系。

## 许可

本仓库中的 demo 以 **Apache-2.0** 许可（见 [LICENSE](LICENSE)）。完整的 memexa 引擎是独立的专有产品，不在该许可覆盖范围内。
