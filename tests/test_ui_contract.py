"""单图模式进度反馈的页面契约（静态断言，不启动服务、不跑推理）。"""
from __future__ import annotations

from pathlib import Path

INDEX = Path(__file__).resolve().parent.parent / "static" / "index.html"


def _html() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_progress_ui_elements_exist():
    html = _html()
    for token in ('id="runState"', 'id="runLabel"', 'id="runTimer"', 'id="doneNote"'):
        assert token in html, f"缺少进度反馈元素：{token}"
    assert "@keyframes slide" in html, "进度条需要可见动画（阻塞请求期间页面不假死）"


def test_progress_ui_is_hidden_until_a_run_starts():
    html = _html()
    assert '<div id="runState" class="runstate" hidden>' in html
    assert '<div id="doneNote" class="oknote" hidden></div>' in html


def test_single_run_shows_running_timer_and_completion():
    html = _html()
    assert "beginRun('RUNNING · 上传与推理中')" in html
    assert "$('runTimer').textContent = ((performance.now() - runStart) / 1000).toFixed(1) + 's'" in html
    assert "endRun('ok'," in html and "透明 PNG 已生成，可下载" in html
    assert "fetch('/api/matting/single'" in html


def test_not_ready_semantics_preserved():
    html = _html()
    assert "'模型未就绪（'" in html
    assert "不会用假图冒充结果" in html
    assert "s.state !== 'READY' || selected.length === 0" in html, "NOT_READY 时按钮必须禁用"


def test_batch_retry_only_for_terminal_failures():
    html = _html()
    assert "j.state === 'failed' || j.state === 'invalid'" in html
    assert "重试会新建任务" in html
