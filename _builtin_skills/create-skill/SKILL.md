---
name: create-skill
description: Create a new skill by generating SKILL.md with proper frontmatter and instructions
---

# Create Skill

You are helping the user create a new MiniHermes skill.

## Steps

1. Ask the user (via the clarify tool) what the skill should do, unless they already specified it.
2. Determine a short kebab-case name for the skill (e.g. `code-review`, `git-commit`, `summarize`).
3. Write a clear, one-line description (max 100 chars).
4. Write the skill instructions — these are the directions a future agent will follow when the skill is loaded. Write them as if you are instructing another AI agent.
5. Create the skill directory and SKILL.md file:

```
~/.minihermes/skills/<skill-name>/SKILL.md
```

## SKILL.md Format

The file MUST follow this exact format:

```markdown
---
name: <kebab-case-name>
description: <one-line description of what the skill does>
---

# <Skill Title>

<Full instructions for the agent when this skill is loaded.>
```

## Guidelines for Writing Good Skill Instructions

- Be specific and actionable — tell the agent exactly what to do, not vague goals
- Include step-by-step workflow if the task has multiple phases
- Specify output format expectations (e.g. "respond in markdown", "create a file at ...")
- Mention which tools the agent should use (bash, read_file, write_file, etc.)
- Include constraints and edge cases to handle
- Keep instructions concise — under 2000 characters is ideal

## After Creation

After writing the SKILL.md file, inform the user:
- The skill is available immediately on next session start
- They can invoke it with `/<skill-name>` or the agent will auto-load it when relevant
- They can edit it at `~/.minihermes/skills/<skill-name>/SKILL.md`
