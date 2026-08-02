# skill_manage — 技能管理

代码: `tools/skill_manage.py` (257行)

---

## Schema 设计

```python
{
    "name": "skill_manage",
    "parameters": {
        "action": {"enum": ["create", "patch", "archive", "list"]},
        "name": {"type": "string", "description": "Skill name (kebab-case)."},
        "description": {"type": "string", "description": "One-line description (max 100 chars)."},
        "body": {"type": "string", "description": "Full SKILL.md content (for create)."},
        "old_string": {"type": "string", "description": "String to replace (for patch)."},
        "new_string": {"type": "string", "description": "Replacement (for patch)."}
    },
    "required": ["action"]
}
```

## 各 Action 实现

### create

```python
def _create_skill(name, description, body):
    # 1. 校验
    if not re.match(r'^[a-z0-9-]+$', name):    # kebab-case
        return "Error: name must be kebab-case"
    if len(name) > 64:
        return "Error: name too long"
    if len(description) > 100:
        return "Error: description too long"
    if len(body) > 4000:
        return "Error: body too long"

    # 2. 计数检查
    auto_skills = _get_auto_skills()
    if len(auto_skills) >= 20:
        return "Error: max 20 auto skills"

    # 3. 不允许覆盖已有技能
    if (skills_dir / name / "SKILL.md").exists():
        return "Error: skill already exists"

    # 4. 写入带 frontmatter 的 SKILL.md
    content = f"""---
name: {name}
description: {description}
source: auto
status: active
created_at: {now}
updated_at: {now}
---

{body}"""
    write_skill(name, content)
    init_usage(name)  # telemetry 初始化
```

### patch

```python
def _patch_skill(name, old_string, new_string):
    # 1. 读当前内容
    content = read_skill(name)

    # 2. old_string 必须恰好出现一次
    if content.count(old_string) != 1:
        return "Error: old_string must appear exactly once"

    # 3. 替换并更新 frontmatter
    new_content = content.replace(old_string, new_string)
    new_content = _update_frontmatter_timestamp(new_content)

    write_skill(name, new_content)
```

### archive

```python
def _archive_skill(name):
    # 1. 只能归档 auto 技能
    if skill["source"] != "auto":
        return "Error: only auto skills can be archived"

    # 2. 只能归档非 pinned 技能
    if skill.get("pinned"):
        return "Error: pinned skills cannot be archived"

    # 3. 更新 frontmatter status
    _update_skill_status(name, "archived")
```

### list

解析所有技能的 YAML frontmatter，显示 name / description / source / status。

## 关键设计点

### Frontmatter 元数据

所有技能文件以 YAML frontmatter 开头：

```yaml
---
name: my-skill
description: A reusable pattern.
source: auto
status: active
created_at: "2026-06-26T10:00:00"
updated_at: "2026-06-26T10:00:00"
---
```

`source` 区分 `auto`（Agent 创建）和 `builtin`（内置）。

### kebab-case 命名

正则 `^[a-z0-9-]+$`，最大 64 字符。防止文件名冲突和 shell 转义问题。

### patch 精确替换

`old_string` 必须在文件中恰好出现一次——类似 `sed 's/old/new/'` 但要求完全匹配，防止意外修改。

### 数量限制

max 20 auto 技能，防止 Agent 无限创建。

### Telemetry 集成

create → `init_usage(name)` 初始化 `.usage.json`。
