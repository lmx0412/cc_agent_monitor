"""
agent_monitor.py — Agent 侧状态写入工具

状态文件结构：
{
  "active":  [<agent>, ...],   # 活跃 session（running / idle）
  "history": [<agent>, ...],   # 最近 5 条已结束（done / terminated / error）
  "updated_at": "..."
}

Session 生命周期：
  PostToolUse  → update_status(running)   # 写入 active
  Stop         → update_status(idle)      # 仍在 active，等待下一轮输入
  PostToolUse  → update_status(running)   # 再次激活，仍在 active
  SessionEnd   → end_session(sessionend)  # 移入 history（done 或 terminated）
"""

import json
import os
import fcntl
import tempfile
import argparse
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

STATUS_FILE = Path.home() / ".claude" / "agent-status.json"
LOCK_FILE = STATUS_FILE.with_suffix(".lock")

MAX_HISTORY = 5


@contextmanager
def _file_lock():
    """Cross-process exclusive lock around the status file.

    Without this, concurrent hook invocations from multiple Claude sessions
    race on read-modify-write and lose updates.
    """
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _read() -> dict:
    if STATUS_FILE.exists():
        try:
            data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            # 兼容旧格式（只有 agents 列表）
            if "agents" in data and "active" not in data:
                return {"active": data["agents"], "history": [], "updated_at": data.get("updated_at", "")}
            return data
        except Exception:
            pass
    return {"active": [], "history": []}


def _write(data: dict):
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Per-process temp file — concurrent writers must not share the same name,
    # otherwise one process's replace() races with another's and fails with
    # FileNotFoundError on the source.
    fd, tmp_path = tempfile.mkstemp(
        prefix=".agent-status-", suffix=".tmp", dir=str(STATUS_FILE.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, STATUS_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def update_status(
    agent_id: str,
    name: str,
    status: str,
    detail: str = "",
    transcript_path: str = "",
    project_dir: str = "",
):
    """
    更新 active 里的 session 状态（running / idle）。
    若 session 不存在，新建并加入 active。
    status 可以是 running（工具调用中）或 done（本轮回复完成，等待下一轮）。
    若 session 已在 history（已终止），忽略——不会把已退出的 session 重新激活。
    """
    now = datetime.now().isoformat(timespec="seconds")
    with _file_lock():
        data = _read()
        active = data.get("active", [])
        history = data.get("history", [])

        # 已终止的 session 不再更新
        if any(h["id"] == agent_id for h in history):
            return

        existing = next((a for a in active if a["id"] == agent_id), None)
        if existing:
            existing["name"] = name
            existing["status"] = status
            if detail:          # idle 时 detail 传空，保留上一轮的内容
                existing["detail"] = detail
            existing["updated_at"] = now
            if transcript_path:
                existing["transcript_path"] = transcript_path
            if project_dir:
                existing["project_dir"] = project_dir
        else:
            active.append({
                "id": agent_id,
                "name": name,
                "status": status,
                "detail": detail,
                "started_at": now,
                "ended_at": None,
                "updated_at": now,
                "transcript_path": transcript_path,
                "project_dir": project_dir,
            })

        data["active"] = active
        data["history"] = history
        data["updated_at"] = now
        _write(data)


def end_session(
    agent_id: str,
    end_status: str,
    name: str = "",
    transcript_path: str = "",
    project_dir: str = "",
):
    """
    将 session 从 active 移入 history。

    end_status:
      "sessionend"  → 根据 active 里的当前状态决定：
                       idle    → history 里标记 "done"
                       running → history 里标记 "terminated"（被强制中断）
      "done"        → 直接标记 done（外部调用用）
      "error"       → 直接标记 error
      "terminated"  → 直接标记 terminated
    """
    now = datetime.now().isoformat(timespec="seconds")
    with _file_lock():
        data = _read()
        active = data.get("active", [])
        history = data.get("history", [])

        agent = next((a for a in active if a["id"] == agent_id), None)

        if agent is None:
            # 不在 active：可能从未有过 PostToolUse（session 没有工具调用就退出）
            # 检查是否已在 history，避免重复
            if any(h["id"] == agent_id for h in history):
                return
            # 构造最小记录
            final_status = _resolve_end_status(end_status, "running")
            agent = {
                "id": agent_id,
                "name": name or agent_id[:8],
                "status": final_status,
                "detail": "",
                "started_at": now,
                "ended_at": now,
                "updated_at": now,
                "transcript_path": transcript_path,
                "project_dir": project_dir,
            }
        else:
            active = [a for a in active if a["id"] != agent_id]
            agent = dict(agent)
            if name:
                agent["name"] = name
            if transcript_path:
                agent["transcript_path"] = transcript_path
            if project_dir:
                agent["project_dir"] = project_dir
            agent["status"] = _resolve_end_status(end_status, agent.get("status", "running"))
            agent["ended_at"] = now
            agent["updated_at"] = now

        history = [agent] + [h for h in history if h["id"] != agent_id]
        history = history[:MAX_HISTORY]

        data["active"] = active
        data["history"] = history
        data["updated_at"] = now
        _write(data)


def _resolve_end_status(end_status: str, current_status: str) -> str:
    """
    将 end_status 解析为最终写入 history 的状态字符串。
    "sessionend" 时根据 active 里的当前状态推断：
      idle    → "done"（正常完成后退出）
      running → "terminated"（回答途中强制退出）
    """
    if end_status == "sessionend":
        return "done" if current_status in ("done", "idle") else "terminated"
    return end_status


def clear_agent(agent_id: str):
    """从 active 和 history 中移除指定 session。"""
    with _file_lock():
        data = _read()
        data["active"] = [a for a in data.get("active", []) if a["id"] != agent_id]
        data["history"] = [a for a in data.get("history", []) if a["id"] != agent_id]
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _write(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Update agent monitor status")
    parser.add_argument("--id", required=True, help="Agent unique ID")
    parser.add_argument("--name", default="", help="Display name")
    parser.add_argument("--status", default="running",
                        choices=["waiting", "running", "idle", "done", "error", "terminated"])
    parser.add_argument("--detail", default="", help="Current detail message")
    parser.add_argument("--transcript", default="", help="Path to Claude Code session JSONL")
    parser.add_argument("--end", default="",
                        choices=["done", "error", "terminated", "sessionend"],
                        help="End the session with this terminal status")
    parser.add_argument("--clear", action="store_true", help="Remove this agent from all lists")
    args = parser.parse_args()

    if args.clear:
        clear_agent(args.id)
    elif args.end:
        end_session(args.id, args.end, name=args.name, transcript_path=args.transcript)
    else:
        update_status(args.id, args.name, args.status, args.detail, args.transcript)
