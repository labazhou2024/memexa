# 把 legacy path 占位迁移到 `_path_resolver`

[English](path_resolver.md) · **中文**

> 背景: 初次脱敏 pass 时, 我们把字面 Windows 路径
> (`C:\Users\<name>\OneDrive\...\claude workspace\...`) 替换成模板占位
> `<USERPROFILE>` 和 `<WORKSPACE_ID>`, 这样代码不会带任何人的个人路径发布。
> 一些模块还含这些占位 token — 它们在文件顶部用 `TODO(memgraph-oss)`
> 注释标记。
>
> 本文档是把那些占位替换成调用 `memexa.core._path_resolver` 的食谱。

## 问题的形态

典型 legacy 文件长这样:

```python
# memexa/core/hard_rule_audit.py
PROJECT_PROJECTS_PATTERN = (
    "<USERPROFILE>/.claude/projects/"
    "<WORKSPACE_ID>/memory"
)
```

原代码意图是 *"用户 `.claude/projects/<workspace-id>/` 树下规范的 memory
目录"*。

迁移后:

```python
from pathlib import Path
from memexa.core._path_resolver import workspace_root

# workspace_root() 默认返 Path(~/.claude/projects/<workspace-id>);
# 用户可通过 MEMEXA_WORKSPACE_ROOT 覆盖
MEMORY_DIR = workspace_root() / "memory"
```

## 步骤

1. 打开文件, 找 `TODO(memgraph-oss)` 标记
2. 找所有用 `<USERPROFILE>` 或 `<WORKSPACE_ID>` 的行
3. 分类用法:
   - **Pattern A — 路径构造**: 字面字串换成 `workspace_root() / "subpath"`
     表达式。没 import 就加 import
   - **Pattern B — regex / glob 模式**: 字串转 compiled `re.Pattern`,
     允许任何用户前缀
   - **Pattern C — 仅显示**: 如果字串最终打给用户看, 换成更诚实的
     `<your-workspace-root>` 占位
4. 删掉 `TODO(memgraph-oss)` 标记
5. 在 `tests/unit/test_<modname>.py` 加 unit test, import 该模块时
   `MEMEXA_WORKSPACE_ROOT` 设成临时目录, assert 解析路径合理

## Pattern A — 路径构造

之前:

```python
DEFAULT_BANK_PATH = "<USERPROFILE>/.claude/data/audit_corpus.jsonl"
```

之后:

```python
from memexa.core._path_resolver import workspace_root

DEFAULT_BANK_PATH = workspace_root().parent / ".claude" / "data" / "audit_corpus.jsonl"
```

便利: `_path_resolver` 已经为这个特定文件暴露了 `audit_corpus_path()`,
优先用:

```python
from memexa.core._path_resolver import audit_corpus_path

DEFAULT_BANK_PATH = audit_corpus_path()
```

## Pattern B — regex / glob 模式

之前:

```python
PROJECT_PROJECTS_PATTERN = (
    "<USERPROFILE>/.claude/projects/"
    "<WORKSPACE_ID>/memory"
)
```

之后:

```python
import re
PROJECT_PROJECTS_PATTERN = re.compile(
    r".claude[\\/]projects[\\/][^\\/]+[\\/]memory(?:[\\/]|$)"
)
```

## Pattern C — 仅显示

之前:

```python
ERROR_MSG = "expected file under <USERPROFILE>/.claude/projects"
```

之后:

```python
ERROR_MSG = "expected file under <your-claude-projects-root>"
```

## 删 TODO 前 checklist

- [ ] 模块 import 了 `workspace_root` (或某个 exported 便利函数)
- [ ] 文件里所有 `<USERPROFILE>` 和 `<WORKSPACE_ID>` 都没了或刻意保留在
  面向用户的消息字串里
- [ ] 测试用临时 workspace 跑通该模块
- [ ] `make pii-scan` 对该文件还是 0 命中
