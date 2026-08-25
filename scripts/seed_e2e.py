from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.startup import build_runtime


def main() -> None:
    data_root = Path(os.getenv("BETTER_AGENT_DATA_ROOT", str(ROOT / "data"))).resolve()
    runtime = build_runtime(data_root)
    thread = runtime.conversation.create_thread("黄金闭环测试")
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    tomorrow = today + timedelta(days=1)
    version = runtime.plan_documents.save_model_revision(
        thread_id=thread.id,
        title="两天行动计划",
        markdown_content="# 两天行动计划\n\n## 目标\n用两天完成一个可验证的小目标。\n\n## 每日安排\n- 每天完成一个行动并记录结果。\n",
        source_turn_id=None,
        source_message_id=None,
        actor="user",
    )
    (data_root / "e2e-seed.json").write_text(
        json.dumps({"thread_id": thread.id, "plan_id": version.plan_document_id, "today": today.isoformat(), "tomorrow": tomorrow.isoformat()}),
        encoding="utf-8",
    )
    print(json.dumps({"thread_id": thread.id, "plan_id": version.plan_document_id, "today": today.isoformat()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
