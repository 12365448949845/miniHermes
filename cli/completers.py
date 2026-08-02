"""输入补全器：斜杠命令补全 + @file: 路径补全。"""

from pathlib import Path

from prompt_toolkit.completion import Completer, Completion

from cli.commands import SLASH_COMMANDS


class SlashCommandCompleter(Completer):
    """斜杠命令补全。"""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        for cmd, description in SLASH_COMMANDS.items():
            if cmd.startswith(text):
                yield Completion(cmd, start_position=-len(text),
                                 display=cmd, display_meta=description)


class FileRefCompleter(Completer):
    """@file: 文件引用路径补全。"""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        idx = text.rfind("@")
        if idx == -1:
            return
        if idx > 0 and text[idx - 1].isalnum():
            return

        after_at = text[idx + 1:]
        if after_at.startswith("file:"):
            path_prefix = after_at[5:]
        else:
            path_prefix = after_at

        if path_prefix and path_prefix[0] in ('"', "'", '`'):
            return
        if " " in path_prefix:
            return

        cwd = Path.cwd()
        base = cwd / path_prefix if path_prefix else cwd
        if path_prefix and not path_prefix.endswith("/"):
            parent = base.parent
            name_prefix = base.name
        else:
            parent = base
            name_prefix = ""

        if not parent.is_dir():
            return

        replace_len = len(text) - idx
        count = 0
        try:
            for entry in sorted(parent.iterdir()):
                if entry.name.startswith(".") or entry.name == "__pycache__":
                    continue
                if name_prefix and not entry.name.lower().startswith(name_prefix.lower()):
                    continue
                if count >= 30:
                    break
                rel = entry.relative_to(cwd)
                display_name = str(rel) + ("/" if entry.is_dir() else "")
                yield Completion(f"@file:{display_name}", start_position=-replace_len,
                                 display=display_name,
                                 display_meta="dir" if entry.is_dir() else f"{entry.stat().st_size}B")
                count += 1
        except OSError:
            return
