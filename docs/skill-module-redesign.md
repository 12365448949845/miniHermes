# MiniHermes Skill 模块改造方案

## 背景

当前 MiniHermes 的 skill 系统参考了 hermes-agent 早期版本的骨架，但只实现了最基础的"发现→加载→返回"链。与 claude-code 和 hermes-agent（最新版）相比，存在以下核心差距：

| 能力 | MiniHermes | claude-code | hermes-agent |
|------|-----------|-------------|--------------|
| skill_dir 路径暴露给 agent | ❌ | ✅ prompt首行注入 | ✅ JSON `skill_dir` 字段 |
| 同目录子文件读取 | ❌ | ❌（依赖 shell） | ✅ `skill_view(name, file_path)` |
| `${SKILL_DIR}` 模板替换 | ❌ | ✅ `${CLAUDE_SKILL_DIR}` | ✅ `${HERMES_SKILL_DIR}` |
| `!`cmd`` 内联 shell 执行 | ❌ | ✅ | ✅ |
| 参数替换 | ❌ | ✅ `$ARGUMENTS` | ❌ |
| skill_dir 子目录索引 | ❌ | ❌ | ✅ references/templates/assets/scripts |
| `skill_manage create` 创建骨架 | ❌ 仅 SKILL.md | N/A | N/A |

改造目标：在保持 MiniHermes 简洁风格的前提下，补齐以上能力。

## 架构概览

```
改造前:
  skill_view(name) → load_skill() → body 字符串 → agent

改造后:
  skill_view(name [, file_path])
    → load_skill(name) → { body, skill_dir, linked_files } → 构造 agent 可理解的消息
    → skill_dir 注入到 body 头部，agent 可直接用 bash/read_file 访问子文件
    → file_path 不为空时，读取 skill_dir/file_path 并返回
```

## 实施计划

### 阶段一：最小可行改动（破坏性 0，收益最大）

改动文件: `skills/manager.py`, `tools/skills_tool.py`

#### 1.1 load_skill() 返回 skill_dir

当前 `load_skill()` 只返回 body 文本。改为也返回 skill_dir，或者直接在 body 头部注入路径信息。

**方案**（选择注入方案，最简洁）:

```python
def load_skill(name: str) -> Optional[str]:
    # ... 现有查找逻辑不变 ...
    if skill_name == name:
        skill_dir = skill_md.parent
        body = substitute_placeholders(body.strip(), skill_dir)
        return f"Skill directory: {skill_dir}\n\n{body}"
```

agent 看到 `Skill directory: /Users/.../.minihermes/skills/my-skill` 后就可以用 `read_file` 或 `bash` 访问该目录下的任意文件。

**向后兼容**: 完全兼容。所有现有 skill 不受影响，只是返回内容多了一行目录提示。

#### 1.2 `${SKILL_DIR}` 占位符替换

在 `load_skill()` 内部增加一个轻量替换函数：

```python
def _apply_placeholders(body: str, skill_dir: Path) -> str:
    return body.replace("${SKILL_DIR}", str(skill_dir))
```

这样 SKILL.md 里可以写 `${SKILL_DIR}/scripts/helper.py`，加载时自动替换为绝对路径。

### 阶段二：子文件加载（支持 skill_view(name, file_path)）

改动文件: `tools/skills_tool.py`, `skills/manager.py`

#### 2.1 schema 扩展

```python
_SCHEMA = {
    "name": "skill_view",
    "parameters": {
        "properties": {
            "name": {"type": "string"},
            "file_path": {  # 新增
                "type": "string",
                "description": "Optional relative path within the skill directory"
            }
        },
        "required": ["name"]
    }
}
```

#### 2.2 skill_view() 实现

```python
def skill_view(name: str, file_path: str = None) -> str:
    # ... 现有查找逻辑 ...
    if file_path and skill_dir:
        # 路径穿越防护
        target = (skill_dir / file_path).resolve()
        if not str(target).startswith(str(skill_dir.resolve())):
            return "Error: path traversal denied"
        if not target.is_file():
            return f"Error: '{file_path}' not found. Available: {list_files(skill_dir)}"
        content = target.read_text(encoding="utf-8")
        return content  # 直接返回文件内容
    # ... 现有主 SKILL.md 返回逻辑 ...
```

#### 2.3 子文件上报（Linked Files 索引）

在 skill_view 返回 body 前追加一个子文件清单：

```python
# 扫描 skill_dir 下非 SKILL.md 的文件
extra_files = []
for f in skill_dir.rglob("*"):
    if f.is_file() and f.name != "SKILL.md":
        rel = f.relative_to(skill_dir)
        extra_files.append(str(rel))

if extra_files:
    body += f"\n\n# Companion files (read with skill_view '{name}', file_path='...'):\n"
    for ef in sorted(extra_files)[:20]:
        body += f"#   {ef}\n"
```

优势：agent 在加载 skill 时就能看到有哪些附属文件，可以自主决定要不要加载它们。

### 阶段三：skill_manage 骨架生成

改动文件: `tools/skill_manage.py`

#### 3.1 创建标准子目录骨架

```python
def _create_skill(name, description, body):
    # ... 现有校验逻辑 ...
    skill_dir = USER_SKILLS_DIR / name
    skill_dir.mkdir(parents=True, exist_ok=True)

    # 创建标准骨架目录
    for subdir in ["scripts", "references", "assets", "templates"]:
        (skill_dir / subdir).mkdir(exist_ok=True)

    # 每个子目录加 .gitkeep
    for subdir in ["scripts", "references", "assets", "templates"]:
        (skill_dir / subdir / ".gitkeep").write_text("")

    # 写入 SKILL.md（现有逻辑）
    skill_md.write_text(frontmatter + body)
```

#### 3.2 body 长度限制放宽

当前 `_MAX_BODY_CHARS = 4000` 对于需要嵌入代码的 skill 偏小。建议提升到 8000（与 write_file 工具限制一致）。

### 阶段四：可选增强（按需）

#### 4.1 `!`cmd`` 内联 shell 执行

在 `load_skill()` 的 `_apply_placeholders()` 之后添加：

```python
_INLINE_SHELL_RE = re.compile(r"!`([^`\n]+)`")

def _expand_inline_shell(body: str, cwd: Path) -> str:
    if "!`" not in body:
        return body
    
    def _run(match):
        cmd = match.group(1).strip()
        try:
            result = subprocess.run(
                ["bash", "-c", cmd],
                cwd=str(cwd), capture_output=True, text=True,
                timeout=5, check=False
            )
            output = (result.stdout or result.stderr).strip()
            return output[:2000]  # 截断
        except Exception as e:
            return f"[inline-shell error: {e}]"
    
    return _INLINE_SHELL_RE.sub(_run, body)
```

注意：需要在 config 中加开关 `skills.inline_shell: true`，默认关闭（安全考虑）。

#### 4.2 多级加载提示优化

当前 system prompt 中的技能索引是线性的，对于超过 10 个技能的情况可以分组：

```
## Available Skills (Tier 1)
### Code Quality
- code-review: ...
### Operations
- weather-query: ...
```

但这个是锦上添花，不阻塞核心功能。

### 阶段五：文档更新

文件: `docs/tools/skill-view.md`, `docs/tools/skill-manage.md`

更新两个文档反映新增能力：
- skill_view 新增 `file_path` 参数
- SKILL.md 中 `${SKILL_DIR}` 占位符用法
- skill 目录下的 scripts/references/assets/templates 子目录约定
- 创建技能时的骨架生成行为

## 实施顺序与优先级

```
P0（本周）:
  1. load_skill() 注入 skill_dir 到返回内容头部
  2. ${SKILL_DIR} 占位符替换
  影响: 立刻解锁"skill 引用同目录代码/文件"能力

P1（完成 P0 后）:
  3. skill_view(file_path) 子文件读取
  4. 子文件清单上报
  影响: agent 可完全在工具层面操作 skill 附属文件

P2（按需）:
  5. skill_manage 骨架生成
  6. body 长度限制放宽
  影响: agent 创建 skill 时自动生成 scripts/ 子目录，可直接写代码进去

P3（可选）:
  7. !`cmd` 内联 shell（需加开关）
```

## 风险 & 约束

1. **向后兼容**: 所有改动对现有 SKILL.md 透明，只需修改 `manager.py` 和 `skills_tool.py`
2. **路径穿越**: 子文件读取必须用 `resolve()` + 前缀比对防护
3. **Token 消耗**: skill_dir 提示 + 子文件清单会增加少量 token，但远低于"agent 反复猜测路径"带来的浪费
4. **内联 shell 安全**: 默认关闭，开启警告，执行以 skill_dir 为 CWD
