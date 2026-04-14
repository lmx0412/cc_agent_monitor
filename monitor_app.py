"""
monitor_app.py — Agent Monitor 浮动窗口

启动：
    python monitor_app.py

依赖：
    pip install PyQt6
    pip install pyobjc-framework-Cocoa  # 可选，macOS 多桌面支持
"""

import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# macOS: PyObjC for NSWindowCollectionBehavior (optional dependency)
_PYOBJC_AVAILABLE = False
if sys.platform == "darwin":
    try:
        import objc  # noqa: F401
        from AppKit import NSApp  # noqa: F401
        _PYOBJC_AVAILABLE = True
    except ImportError:
        pass

# Token pricing — loaded from pricing.json (same directory as this script).
# Falls back to standard Anthropic claude-sonnet USD rates if the file is missing.
_DEFAULT_PRICE_PER_M = {
    "input":          3.00,
    "cache_creation": 3.75,
    "cache_read":     0.30,
    "output":        15.00,
}

def _load_pricing() -> dict:
    config_path = Path(__file__).parent / "pricing.json"
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        rates = data.get("per_million_tokens", {})
        return {
            "input":          float(rates.get("input",          _DEFAULT_PRICE_PER_M["input"])),
            "cache_creation": float(rates.get("cache_creation", _DEFAULT_PRICE_PER_M["cache_creation"])),
            "cache_read":     float(rates.get("cache_read",     _DEFAULT_PRICE_PER_M["cache_read"])),
            "output":         float(rates.get("output",         _DEFAULT_PRICE_PER_M["output"])),
        }
    except Exception:
        return dict(_DEFAULT_PRICE_PER_M)

_PRICE = _load_pricing()

from PyQt6.QtCore import Qt, QTimer, QPoint, QRectF
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QVBoxLayout,
    QHBoxLayout,
    QWidget,
    QFrame,
)

STATUS_FILE = Path.home() / ".claude" / "agent-status.json"
POSITION_FILE = Path.home() / ".claude" / "monitor-position.json"

# ── 样式常量 ──────────────────────────────────────────────
BG_COLOR        = QColor(18, 18, 24, 250)      # 近乎不透明的深蓝黑
BG_COLOR_INNER  = QColor(24, 24, 32, 250)      # 稍亮，用于行背景
BORDER_COLOR    = QColor(55, 55, 75, 255)      # 带蓝调的边框
ACCENT_COLOR    = QColor(80, 120, 220, 60)     # 蓝色高亮（running 行左边框）
TITLE_COLOR     = "#6b7280"
TEXT_COLOR      = "#e2e8f0"
NAME_COLOR      = "#f1f5f9"
TIME_COLOR      = "#94a3b8"
SECTION_COLOR   = "#4b5563"
HEADER_COLOR    = "#374151"
DETAIL_COLOR    = "#64748b"

STATUS_COLORS = {
    "running":    "#60a5fa",
    "done":       "#4ade80",
    "error":      "#f87171",
    "terminated": "#fb923c",
    "waiting":    "#6b7280",
}

STATUS_ICONS = {
    "running":    "●",
    "done":       "✓",
    "error":      "✗",
    "terminated": "⊘",
    "waiting":    "○",
}

MAX_ACTIVE  = 5
MAX_HISTORY = 5
REFRESH_INTERVAL_MS       = 15_000   # 活跃任务刷新（15秒）
DAILY_REFRESH_INTERVAL_MS = 300_000  # 今日统计刷新（5分钟）
WINDOW_WIDTH = 430


# ── 工具函数 ──────────────────────────────────────────────

def elapsed_minutes(started_at: str, ended_at: str | None) -> str:
    if not started_at:
        return "—"
    try:
        fmt = "%Y-%m-%dT%H:%M:%S"
        start = datetime.strptime(started_at, fmt)
        end = datetime.strptime(ended_at, fmt) if ended_at else datetime.now()
        minutes = max(0, int((end - start).total_seconds() // 60))
        return f"{minutes}m"
    except Exception:
        return "—"


def parse_token_usage(transcript_path: str) -> tuple[int, float]:
    path = Path(transcript_path)
    if not path.exists():
        return 0, 0.0
    try:
        total_input = total_output = total_cache_creation = total_cache_read = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            usage = d.get("message", {}).get("usage", {})
            if usage and d.get("message", {}).get("role") == "assistant":
                total_input          += usage.get("input_tokens", 0)
                total_output         += usage.get("output_tokens", 0)
                total_cache_creation += usage.get("cache_creation_input_tokens", 0)
                total_cache_read     += usage.get("cache_read_input_tokens", 0)
        total_tokens = total_input + total_output + total_cache_creation + total_cache_read
        cost = (
            total_input          * _PRICE["input"] +
            total_cache_creation * _PRICE["cache_creation"] +
            total_cache_read     * _PRICE["cache_read"] +
            total_output         * _PRICE["output"]
        ) / 1_000_000
        return total_tokens, cost
    except Exception:
        return 0, 0.0


def format_tokens(total: int) -> str:
    if total == 0:
        return "—"
    if total < 1_000:
        return str(total)
    if total < 1_000_000:
        return f"{total / 1000:.1f}k".rstrip("0").rstrip(".")
    return f"{total / 1_000_000:.1f}M"


def format_cost(cost: float) -> str:
    if cost == 0.0:
        return ""
    if cost < 0.01:
        return "<$0.01"
    return f"${cost:.2f}"


def _window_sort_key(name: str) -> tuple[int, int]:
    """
    将 'w1 t3' 之类的标签解析为 (window, tab) 用于排序。
    无法解析时返回 (999, 999)，排在最后。
    """
    m = re.match(r"w(\d+)\s+t(\d+)", name or "")
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return (999, 999)


def load_state() -> tuple[list[dict], list[dict]]:
    """读取状态文件，返回 (active_list, history_list)。"""
    if not STATUS_FILE.exists():
        return [], []
    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        # 兼容旧格式
        if "agents" in data and "active" not in data:
            active = data["agents"][:MAX_ACTIVE]
            return active, []
        active  = data.get("active",  [])[:MAX_ACTIVE]
        history = data.get("history", [])[:MAX_HISTORY]
        return active, history
    except Exception:
        return [], []


def parse_today_usage() -> tuple[int, float]:
    """
    扫描 ~/.claude/projects/ 下今日所有 session 的 token 消耗，
    使用 pricing.json 中配置的费率（默认标准 Anthropic sonnet 定价）。
    返回 (total_tokens, total_cost)。
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return 0, 0.0

    total_input = total_output = total_cache_creation = total_cache_read = 0

    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl_file in project_dir.glob("*.jsonl"):
            try:
                for line in jsonl_file.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    # 只统计今日
                    ts = d.get("timestamp", "")
                    if not ts or ts[:10] != today:
                        continue
                    msg = d.get("message", {})
                    if not isinstance(msg, dict) or msg.get("role") != "assistant":
                        continue
                    usage = msg.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    total_input          += int(usage.get("input_tokens", 0) or 0)
                    total_output         += int(usage.get("output_tokens", 0) or 0)
                    total_cache_creation += int(usage.get("cache_creation_input_tokens", 0) or 0)
                    total_cache_read     += int(usage.get("cache_read_input_tokens", 0) or 0)
            except Exception:
                continue

    total_tokens = total_input + total_output + total_cache_creation + total_cache_read
    cost = (
        total_input          * _PRICE["input"] +
        total_cache_creation * _PRICE["cache_creation"] +
        total_cache_read     * _PRICE["cache_read"] +
        total_output         * _PRICE["output"]
    ) / 1_000_000
    return total_tokens, cost


def _tty_to_window_label() -> dict[str, str]:
    """
    通过 AppleScript 查询 iTerm2，返回 {'/dev/ttys001': 'w1 t2', ...} 映射。
    失败时返回空字典。
    """
    script = '''
tell application "iTerm2"
    set output to ""
    set winIdx to 0
    repeat with w in windows
        set winIdx to winIdx + 1
        set tabIdx to 0
        repeat with t in tabs of w
            set tabIdx to tabIdx + 1
            try
                set s to current session of t
                set ttyPath to tty of s
                set output to output & "w" & winIdx & "t" & tabIdx & "|" & ttyPath & "\\n"
            end try
        end repeat
    end repeat
    return output
end tell
'''
    try:
        import subprocess
        result = subprocess.run(
            ["osascript", "-"],
            input=script, capture_output=True, text=True, timeout=3
        )
        mapping: dict[str, str] = {}
        for line in result.stdout.strip().splitlines():
            if "|" not in line:
                continue
            label_raw, tty = line.split("|", 1)
            # label_raw 形如 "w1t2"，转成 "w1 t2"
            m = re.match(r"w(\d+)t(\d+)", label_raw.strip())
            if m:
                label = f"w{m.group(1)} t{m.group(2)}"
                mapping[tty.strip()] = label
        return mapping
    except Exception:
        return {}


def _cwd_to_project_dir(cwd: str) -> Path:
    """将进程 cwd 转成 ~/.claude/projects/ 下的路径。"""
    key = cwd.replace("/", "-").replace("_", "-")
    return Path.home() / ".claude" / "projects" / key


def _latest_jsonl(project_dir: Path) -> Path | None:
    """返回 project_dir 下最近修改的 .jsonl 文件，或 None。"""
    try:
        files = list(project_dir.glob("*.jsonl"))
        if not files:
            return None
        return max(files, key=lambda f: f.stat().st_mtime)
    except Exception:
        return None


def _read_session_id(jsonl_path: Path) -> str:
    """从 JSONL 第一行读取 sessionId 字段。"""
    try:
        first_line = jsonl_path.open(encoding="utf-8").readline()
        d = json.loads(first_line)
        return d.get("sessionId", "")
    except Exception:
        return ""


def recover_active_sessions():
    """
    启动时扫描正在运行的 claude 进程，将未被记录的 session 恢复到 active。
    依赖：ps（获取 pid/tty/cwd）+ AppleScript（tty→窗口标签）。
    """
    import subprocess

    # 1. 获取 tty → 窗口标签 映射
    tty_map = _tty_to_window_label()

    # 2. 读取现有状态，收集已知 session id
    if STATUS_FILE.exists():
        try:
            data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {"active": [], "history": []}
    else:
        data = {"active": [], "history": []}

    active  = data.get("active", [])
    history = data.get("history", [])
    known_ids = {a["id"] for a in active} | {h["id"] for h in history}

    # 3. 枚举 claude 进程
    try:
        ps_out = subprocess.run(
            ["ps", "-eo", "pid,tty,comm"],
            capture_output=True, text=True, timeout=3
        ).stdout
    except Exception:
        return

    now = datetime.now().isoformat(timespec="seconds")
    added = False

    for line in ps_out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid_str, tty_raw, comm = parts
        if "claude" not in comm.lower():
            continue
        try:
            pid = int(pid_str)
        except ValueError:
            continue

        # tty_raw 形如 "s001"，完整路径是 /dev/ttys001
        tty_full = f"/dev/tty{tty_raw}" if not tty_raw.startswith("/") else tty_raw
        window_label = tty_map.get(tty_full, "")

        # 4. 找 cwd
        try:
            lsof_out = subprocess.run(
                ["lsof", "-p", str(pid), "-a", "-d", "cwd", "-Fn"],
                capture_output=True, text=True, timeout=3
            ).stdout
            cwd = ""
            for lsof_line in lsof_out.splitlines():
                if lsof_line.startswith("n"):
                    cwd = lsof_line[1:]
                    break
        except Exception:
            continue

        if not cwd:
            continue

        # 5. 找最新 transcript
        proj_dir = _cwd_to_project_dir(cwd)
        jsonl = _latest_jsonl(proj_dir)
        if jsonl is None:
            continue

        session_id = _read_session_id(jsonl)
        if not session_id:
            continue

        name = window_label or f"pid{pid}"

        # 已在 active：只补全 window_label（可能之前没有）
        existing_active = next((a for a in active if a["id"] == session_id), None)
        if existing_active:
            if window_label and not existing_active.get("name", "").startswith("w"):
                existing_active["name"] = window_label
                added = True
            continue

        # 已在 history：进程还活着说明 session 被错误地移入了 history（旧 Stop 逻辑遗留）
        # 将其移回 active，标记为 idle
        existing_hist = next((h for h in history if h["id"] == session_id), None)
        if existing_hist:
            history = [h for h in history if h["id"] != session_id]
            restored = dict(existing_hist)
            restored["status"] = "done"
            restored["ended_at"] = None
            restored["updated_at"] = now
            if window_label:
                restored["name"] = window_label
            active.append(restored)
            known_ids.add(session_id)
            added = True
            continue

        # 完全未知：新建 active 记录
        active.append({
            "id": session_id,
            "name": name,
            "status": "done",
            "detail": "",
            "started_at": now,
            "ended_at": None,
            "updated_at": now,
            "transcript_path": str(jsonl),
            "project_dir": cwd,
        })
        known_ids.add(session_id)
        added = True

    if added:
        data["active"] = active
        data["history"] = history
        data["updated_at"] = now
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATUS_FILE)


def load_position() -> QPoint | None:
    if POSITION_FILE.exists():
        try:
            d = json.loads(POSITION_FILE.read_text())
            return QPoint(d["x"], d["y"])
        except Exception:
            pass
    return None


def save_position(pos: QPoint):
    POSITION_FILE.write_text(json.dumps({"x": pos.x(), "y": pos.y()}))


# ── 单行 Agent 组件 ───────────────────────────────────────

class AgentRow(QWidget):
    """
    单行（history）或双行（active）显示组件。

    active 布局（两行）：
      行1: [icon] [w1 t2]  [status]  [Xm]  [7.5k $0.12]
      行2:        [detail 摘要文字（灰色小字）]

    history 布局（单行）：
      [icon] [project-name]  [status]  [Xm]  [7.5k $0.12]
    """

    def __init__(self, mode: str = "active"):
        super().__init__()
        self.mode = mode
        self._current_status = "waiting"

        font_mono   = QFont("Menlo", 11)
        font_name   = QFont("SF Pro Display", 12)
        font_name.setWeight(QFont.Weight.Medium)
        font_status = QFont("SF Pro Text", 10)
        font_time   = QFont("Menlo", 10)
        font_detail = QFont("SF Pro Text", 10)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 3, 0, 3)
        outer.setSpacing(2)

        # ── 主行 ──
        main_row = QHBoxLayout()
        main_row.setContentsMargins(14, 0, 14, 0)
        main_row.setSpacing(0)

        self.icon_label = QLabel("·")
        self.icon_label.setFont(font_mono)
        self.icon_label.setFixedWidth(16)

        self.name_label = QLabel("—")
        self.name_label.setFont(font_name)
        self.name_label.setStyleSheet(f"color: {NAME_COLOR};")

        self.status_label = QLabel("")
        self.status_label.setFont(font_status)
        self.status_label.setFixedWidth(72)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.time_label = QLabel("—")
        self.time_label.setFont(font_time)
        self.time_label.setFixedWidth(38)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.time_label.setStyleSheet(f"color: {TIME_COLOR};")

        self.token_label = QLabel("—")
        self.token_label.setFont(font_time)
        self.token_label.setFixedWidth(80)
        self.token_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.token_label.setStyleSheet(f"color: {TIME_COLOR};")

        main_row.addWidget(self.icon_label)
        main_row.addSpacing(6)
        main_row.addWidget(self.name_label, stretch=1)
        main_row.addWidget(self.status_label)
        main_row.addSpacing(6)
        main_row.addWidget(self.time_label)
        main_row.addSpacing(6)
        main_row.addWidget(self.token_label)

        outer.addLayout(main_row)

        # ── detail 行（仅 active 模式）──
        if mode == "active":
            detail_row = QHBoxLayout()
            detail_row.setContentsMargins(36, 0, 14, 0)
            detail_row.setSpacing(0)

            self.detail_label = QLabel("")
            self.detail_label.setFont(font_detail)
            self.detail_label.setStyleSheet(f"color: {DETAIL_COLOR};")
            self.detail_label.setWordWrap(False)

            detail_row.addWidget(self.detail_label, stretch=1)
            outer.addLayout(detail_row)
        else:
            self.detail_label = None

    def refresh(self, agent: dict):
        status = agent.get("status", "waiting")
        color  = STATUS_COLORS.get(status, STATUS_COLORS["waiting"])
        icon   = STATUS_ICONS.get(status, "·")

        prev_status = self._current_status
        self._current_status = status
        if prev_status != status:
            self.update()  # repaint for accent bar

        self.icon_label.setText(icon)
        self.icon_label.setStyleSheet(f"color: {color};")

        if self.mode == "active":
            name = agent.get("name", "")[:12]
        else:
            project_dir = agent.get("project_dir", "")
            name = Path(project_dir).name if project_dir else agent.get("name", "")
            name = name[:20]

        self.name_label.setText(name or "—")
        self.name_label.setStyleSheet(f"color: {NAME_COLOR};")

        if self.mode == "history":
            self.status_label.setText("")
            self.status_label.setVisible(False)
        else:
            self.status_label.setText(status)
            self.status_label.setStyleSheet(f"color: {color}; font-size: 10px;")
            self.status_label.setVisible(True)

        ended_at   = agent.get("ended_at")
        started_at = agent.get("started_at", "")
        time_str   = elapsed_minutes(started_at, ended_at) if status != "waiting" else "—"
        self.time_label.setText(time_str)
        self.time_label.setStyleSheet(f"color: {TIME_COLOR};")

        transcript_path = agent.get("transcript_path", "")
        if transcript_path and status != "waiting":
            total_tokens, cost = parse_token_usage(transcript_path)
            tok_str  = format_tokens(total_tokens)
            cost_str = format_cost(cost)
            self.token_label.setText(f"{tok_str} {cost_str}".strip())
        else:
            self.token_label.setText("—")
        self.token_label.setStyleSheet(f"color: {TIME_COLOR};")

        if self.detail_label is not None:
            detail = agent.get("detail", "").strip()
            if detail:
                max_chars = 42
                display = detail[:max_chars] + ("…" if len(detail) > max_chars else "")
                self.detail_label.setText(display)
                self.detail_label.setVisible(True)
            else:
                self.detail_label.setText("")
                self.detail_label.setVisible(False)

    def paintEvent(self, event):
        """running 状态时绘制左侧蓝色高亮条。"""
        super().paintEvent(event)
        if self._current_status == "running":
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.fillRect(0, 2, 3, self.height() - 4, QColor(96, 165, 250, 200))
            painter.end()


def _make_section_label(text: str) -> QLabel:
    font = QFont("SF Pro Text", 9)
    font.setWeight(QFont.Weight.Medium)
    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
    lbl = QLabel(text)
    lbl.setFont(font)
    lbl.setStyleSheet(
        f"color: {SECTION_COLOR}; padding-left: 14px; padding-top: 6px; padding-bottom: 2px;"
    )
    lbl.setFixedHeight(24)
    return lbl


def _make_header_row() -> QWidget:
    """列头：Session  Status  Time  Tokens  Cost"""
    widget = QWidget()
    widget.setFixedHeight(18)
    row = QHBoxLayout(widget)
    row.setContentsMargins(14, 0, 14, 0)
    row.setSpacing(0)

    font = QFont("SF Pro Text", 9)
    col = HEADER_COLOR

    def lbl(text, width=None, align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter):
        l = QLabel(text)
        l.setFont(font)
        l.setStyleSheet(f"color: {col};")
        if width:
            l.setFixedWidth(width)
        l.setAlignment(align)
        return l

    right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

    row.addSpacing(22)
    row.addWidget(lbl("Session"), 1)
    row.addWidget(lbl("Status",  72, right))
    row.addSpacing(6)
    row.addWidget(lbl("Time",    38, right))
    row.addSpacing(6)
    row.addWidget(lbl("Tokens / Cost", 80, right))
    return widget


def _make_divider() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet(
        f"background-color: {BORDER_COLOR.name()}; border: none; max-height: 1px; margin: 4px 0px;"
    )
    line.setFixedHeight(1)
    return line


# ── 主窗口 ────────────────────────────────────────────────

class MonitorWindow(QWidget):
    def __init__(self):
        super().__init__()
        self._drag_pos = None
        self._setup_window()
        self._build_ui()
        self._start_timer()
        self.refresh()

    def _setup_window(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # macOS: 防止 Tool 窗口在切换焦点时被隐藏
        if sys.platform == "darwin":
            self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.setFixedWidth(WINDOW_WIDTH)

        pos = load_position()
        if pos:
            self.move(pos)
        else:
            screen = QApplication.primaryScreen().availableGeometry()
            self.move(screen.right() - WINDOW_WIDTH - 20, screen.bottom() - 260)

    def _setup_macos_collection_behavior(self):
        """
        通过 PyObjC 设置 NSWindowCollectionBehavior，让窗口出现在所有 Space
        且不随焦点切换而隐藏。需要在 show() 之后调用（winId 此时才有效）。
        """
        if not _PYOBJC_AVAILABLE or sys.platform != "darwin":
            return
        try:
            from AppKit import NSApp
            import objc  # noqa: F401

            win_id = int(self.winId())
            ns_window = None
            for win in NSApp.windows():
                if int(win.windowNumber()) == win_id:
                    ns_window = win
                    break
            if ns_window is None:
                ns_window = NSApp.windows()[-1]

            # NSWindowCollectionBehaviorCanJoinAllSpaces = 1 << 0
            # NSWindowCollectionBehaviorStationary       = 1 << 3
            # NSWindowCollectionBehaviorIgnoresCycle     = 1 << 5
            behavior = (1 << 0) | (1 << 3) | (1 << 5)
            ns_window.setCollectionBehavior_(behavior)
            ns_window.setLevel_(3)  # NSFloatingWindowLevel
        except Exception as e:
            print(f"[monitor] macOS collection behavior setup failed: {e}", file=sys.stderr)

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._container = QWidget()
        self._container.setObjectName("container")
        self._container.setStyleSheet("QWidget#container { background: transparent; }")

        self._inner = QVBoxLayout(self._container)
        self._inner.setContentsMargins(0, 8, 0, 8)
        self._inner.setSpacing(0)

        # 标题行
        title_row = QHBoxLayout()
        title_row.setContentsMargins(14, 2, 14, 2)

        title_font = QFont("SF Pro Display", 11)
        title_font.setWeight(QFont.Weight.Medium)
        title = QLabel("Agent Monitor")
        title.setFont(title_font)
        title.setStyleSheet(f"color: {TITLE_COLOR};")

        self._updated_label = QLabel("")
        self._updated_label.setFont(QFont("Menlo", 10))
        self._updated_label.setStyleSheet(f"color: {TITLE_COLOR};")
        self._updated_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        title_row.addWidget(title)
        title_row.addStretch()
        title_row.addWidget(self._updated_label)
        self._inner.addLayout(title_row)

        # ── 今日统计栏 ──
        daily_row = QHBoxLayout()
        daily_row.setContentsMargins(14, 2, 14, 4)
        daily_row.setSpacing(0)

        daily_font = QFont("Menlo", 10)
        self._daily_label = QLabel("今日: 统计中…")
        self._daily_label.setFont(daily_font)
        self._daily_label.setStyleSheet(f"color: {TIME_COLOR};")

        daily_row.addWidget(self._daily_label)
        daily_row.addStretch()
        self._inner.addLayout(daily_row)

        self._inner.addWidget(_make_divider())

        # ── ACTIVE 区 ──
        self._active_section = _make_section_label("ACTIVE")
        self._inner.addWidget(self._active_section)

        self._header_row = _make_header_row()
        self._inner.addWidget(self._header_row)

        self._active_rows: list[AgentRow] = []
        self._active_container = QWidget()
        self._active_layout = QVBoxLayout(self._active_container)
        self._active_layout.setContentsMargins(0, 0, 0, 0)
        self._active_layout.setSpacing(0)
        self._inner.addWidget(self._active_container)

        # ── 分隔线 + HISTORY 区 ──
        self._history_divider = _make_divider()
        self._inner.addWidget(self._history_divider)

        self._history_section = _make_section_label("HISTORY")
        self._inner.addWidget(self._history_section)

        self._history_rows: list[AgentRow] = []
        self._history_container = QWidget()
        self._history_layout = QVBoxLayout(self._history_container)
        self._history_layout.setContentsMargins(0, 0, 0, 0)
        self._history_layout.setSpacing(0)
        self._inner.addWidget(self._history_container)

        outer.addWidget(self._container)

    def _start_timer(self):
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(REFRESH_INTERVAL_MS)

        # 今日统计：慢速刷新（5分钟），启动时立即执行一次
        self._daily_timer = QTimer(self)
        self._daily_timer.timeout.connect(self.refresh_daily)
        self._daily_timer.start(DAILY_REFRESH_INTERVAL_MS)
        # 延迟 500ms 执行首次统计，避免阻塞启动
        QTimer.singleShot(500, self.refresh_daily)

    def _rebuild_rows(self, layout, rows: list, agents: list[dict], mode: str):
        """按需增删行，避免每次全量重建导致闪烁。"""
        # 增加行
        while len(rows) < len(agents):
            row = AgentRow(mode=mode)
            rows.append(row)
            layout.addWidget(row)
        # 隐藏多余行（不删除，复用）
        for i, row in enumerate(rows):
            if i < len(agents):
                row.refresh(agents[i])
                row.setVisible(True)
            else:
                row.setVisible(False)

    def refresh_daily(self):
        """扫描今日全局 token 消耗，更新统计栏。"""
        total_tokens, cost = parse_today_usage()
        tok_str  = format_tokens(total_tokens)
        cost_str = format_cost(cost)
        date_str = datetime.now().strftime("%m/%d")
        if total_tokens == 0:
            text = f"今日 {date_str}: 暂无数据"
        else:
            text = f"今日 {date_str}:  {tok_str}  {cost_str}"
        self._daily_label.setText(text)

    def refresh(self):
        active, history = load_state()

        # active 区：按窗口编号排序
        active_sorted = sorted(active, key=lambda a: _window_sort_key(a.get("name", "")))
        self._rebuild_rows(self._active_layout, self._active_rows, active_sorted, "active")

        # 无活跃任务时隐藏 ACTIVE 标题和列头
        has_active = bool(active_sorted)
        self._active_section.setVisible(has_active)
        self._header_row.setVisible(has_active)

        # history 区
        self._rebuild_rows(self._history_layout, self._history_rows, history, "history")

        # 无历史时隐藏 HISTORY 区和分隔线
        has_history = bool(history)
        self._history_divider.setVisible(has_history)
        self._history_section.setVisible(has_history)

        now = datetime.now().strftime("%H:%M")
        self._updated_label.setText(now)
        self.adjustSize()

    # ── 圆角背景 ──
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(self.rect().adjusted(1, 1, -1, -1))
        path = QPainterPath()
        path.addRoundedRect(rect, 12, 12)

        # 微渐变：顶部稍亮，底部更深
        gradient = QLinearGradient(0, 0, 0, self.height())
        gradient.setColorAt(0.0, QColor(26, 26, 36, 252))
        gradient.setColorAt(1.0, QColor(16, 16, 22, 252))

        painter.fillPath(path, gradient)

        # 边框：上半段稍亮，营造内嵌感
        painter.setPen(QColor(65, 65, 90, 200))
        painter.drawPath(path)

    # ── 拖动支持 ──
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = None
            save_position(self.pos())


# ── 入口 ─────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Agent Monitor")

    recover_active_sessions()

    window = MonitorWindow()
    window.show()
    window._setup_macos_collection_behavior()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
