"""First bounded procedure learner: user-confirmed diagnose/practice/review."""
import io
import json
import re
import zipfile

METHOD = "先诊断薄弱点，再练习，最后复查"
NAME = "learned-practice-review"


def matches(text):
    return bool(re.search(r"学习|练习|复习", text)) and not bool(re.search(r"(?:不要|不需要|无需).*(?:学习|练习|复习)", text))


def package(version):
    manifest = {"schema_version": 1, "name": NAME, "version": version, "title": "诊断、练习与复查",
                "description": "在学习与练习任务中复用用户确认有效的方法。", "kind": "instruction_only",
                "requested_tools": [], "connectors": [], "phases": ["conversation", "planner", "executor"], "entry_document": "SKILL.md"}
    content = """# 诊断、练习与复查
适用：用户需要学习、练习或复习，且未要求跳过诊断；遵循本次任务的时长与交付约束。
1. 先根据用户提供的结果诊断薄弱点；材料不足时明确未知，不能虚构测评结果。
2. 围绕已识别的薄弱点安排练习，保留用户必需的交付，并遵守时长上限。
3. 复查练习结果，说明仍未解决的问题和下一步。
输出：诊断依据、练习安排、复查标准。没有结果时只给复查标准，不宣称已经有效。
退出：任务无关、前置材料不足或用户改用其他方法时停止应用；沿用既有工具授权。
"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in (("skill.json", json.dumps(manifest, ensure_ascii=False)), ("SKILL.md", content)):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            archive.writestr(info, body)
    return buffer.getvalue()
