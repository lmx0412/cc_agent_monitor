"""
hook_agent_monitor.py — Claude Code Hook 入口脚本

被 ~/.claude/settings.json 的 PostToolUse / Stop / SessionEnd Hook 调用。

Hook 调用方式（settings.json 里配置）：
    python3 /path/to/hook_agent_monitor.py --event posttooluse
    python3 /path/to/hook_agent_monitor.py --event stop
    python3 /path/to/hook_agent_monitor.py --event sessionend

事件语义：
    posttooluse  → session 活跃，更新 running 状态
    stop         → Claude 完成一轮回复（可能还会继续），标记 done
                   但若 SessionEnd 随后触发，done 会被覆盖为 terminated
    sessionend   → CC 进程退出（Ctrl+D / 窗口关闭），标记 terminated
"""

import json
import os
import re
import sys
import argparse
from pathlib import Path

# 确保能 import 同目录的 agent_monitor
sys.path.insert(0, str(Path(__file__).parent))
from agent_monitor import update_status, end_session


def _extract_user_message_text(content) -> str:
    """从 message content（字符串或列表）中提取纯文本。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "").strip()
    return ""


def _iterm_window_label() -> str:
    """
    从 ITERM_SESSION_ID 环境变量解析 iTerm2 窗口/tab 编号。
    格式：w{window}t{tab}p{pane}:UUID，例如 w0t2p0:E5CD...
    返回类似 "w1 t3"（1-indexed）的标签；无法解析时返回空字符串。
    """
    session_id = os.environ.get("ITERM_SESSION_ID", "")
    if not session_id:
        return ""
    m = re.match(r"[wW](\d+)[tT](\d+)[pP](\d+)", session_id)
    if not m:
        return ""
    win = int(m.group(1)) + 1   # 转为 1-indexed
    tab = int(m.group(2)) + 1
    pane = int(m.group(3))
    if pane > 0:
        return f"w{win} t{tab} p{pane + 1}"
    return f"w{win} t{tab}"


def _last_user_message(transcript_path: str, max_chars: int = 40) -> str:
    """从 JSONL 中提取最后一条用户消息的前 max_chars 个字符。"""
    path = Path(transcript_path)
    if not path.exists():
        return ""
    try:
        last_msg = ""
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("message", {}).get("role") != "user":
                continue
            text = _extract_user_message_text(d["message"].get("content", ""))
            if text:
                last_msg = text
        last_msg = last_msg.replace("\n", " ").replace("\r", "")
        return last_msg[:max_chars] + ("…" if len(last_msg) > max_chars else "")
    except Exception:
        return ""


def _resolve_ids(stdin_data: dict) -> tuple[str, str, str]:
    """从 stdin_data 和环境变量中解析 session_id, transcript_path, project_dir。"""
    session_id = (
        stdin_data.get("session_id")
        or os.environ.get("CLAUDE_SESSION_ID", "")
        or os.environ.get("ECC_SESSION_ID", "")
    )
    transcript_path = (
        stdin_data.get("transcript_path")
        or os.environ.get("CLAUDE_TRANSCRIPT_PATH", "")
    )
    project_dir = (
        stdin_data.get("cwd")
        or os.environ.get("CLAUDE_PROJECT_DIR", "")
        or os.environ.get("PWD", "")
    )
    return session_id, transcript_path, project_dir


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--event",
                        choices=["posttooluse", "stop", "sessionend"],
                        required=True)
    args = parser.parse_args()

    stdin_data = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            stdin_data = json.loads(raw)
    except Exception:
        pass

    session_id, transcript_path, project_dir = _resolve_ids(stdin_data)

    if not session_id:
        sys.exit(0)

    # name 优先级：iTerm2 窗口编号 > 项目目录名 > session_id 前缀
    name = (
        _iterm_window_label()
        or (Path(project_dir).name if project_dir else session_id[:8])
    )

    if args.event == "sessionend":
        # CC 进程真正退出（Ctrl+D / 窗口关闭）：移入 history
        # 若之前是 idle（Stop 后等待），标记 done；若是 running，标记 terminated
        end_session(session_id, "sessionend",
                    name=name, transcript_path=transcript_path,
                    project_dir=project_dir)

    elif args.event == "stop":
        # Claude 完成一轮回复，但进程仍在：在 active 里标记为 done（等待下一轮）
        update_status(session_id, name, "done",
                      detail="", transcript_path=transcript_path,
                      project_dir=project_dir)

    else:
        # posttooluse：session 活跃，更新 running 状态
        detail = _last_user_message(transcript_path)
        update_status(session_id, name, "running",
                      detail=detail, transcript_path=transcript_path,
                      project_dir=project_dir)


if __name__ == "__main__":
    main()
