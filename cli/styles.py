"""prompt_toolkit 样式定义。"""

from prompt_toolkit.styles import Style as PTStyle

STYLE = PTStyle.from_dict({
    "input-area":                     "#FFF8DC",
    "input-rule":                     "#CD7F32",
    "prompt":                         "bold #FFD700",
    "status-bar":                     "bg:#1a1a2e #C0C0C0",
    "placeholder":                    "#555555 italic",
    "completion-menu.completion":     "bg:#1e1e2e #cdd6f4",
    "completion-menu.completion.current": "bg:#313244 #FFD700 bold",
    "completion-menu.meta.completion":    "bg:#1e1e2e #6c7086",
    "completion-menu.meta.completion.current": "bg:#313244 #B8860B",
    "clarify-border":                 "#CD7F32",
    "clarify-title":                  "bold #FFD700",
    "clarify-question":               "#FFF8DC",
    "clarify-choice":                 "#C0C0C0",
    "clarify-selected":               "bold #FFD700",
    "clarify-hint":                   "#555555 italic",
    "approval-border":                "#CD7F32",
    "approval-title":                 "bold #FF8C00",
    "approval-desc":                  "#FFF8DC bold",
    "approval-cmd":                   "#AAAAAA italic",
    "approval-choice":                "#C0C0C0",
    "approval-selected":              "bold #FFD700",
    "approval-hint":                  "#555555 italic",
})
