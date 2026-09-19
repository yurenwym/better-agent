"""Validate explicit stage/day tables; prose-only calendars are not inferred."""
from __future__ import annotations

from datetime import date
import re

from markdown_it import MarkdownIt


class PlanCalendarMismatch(ValueError):
    pass


def validate_plan_calendar(markdown: str) -> None:
    if "学习日" not in markdown or "日期" not in markdown:
        return
    tokens=MarkdownIt("commonmark").enable("table").parse(markdown)
    tables=[]
    rows=[]
    row=[]
    for token in tokens:
        if token.type=="table_open":rows=[]
        elif token.type=="tr_open":row=[]
        elif token.type=="inline" and token.level>=4:
            row.append("".join(child.content for child in token.children or [] if child.type in {"text","code_inline"}).strip())
        elif token.type=="tr_close":rows.append(row)
        elif token.type=="table_close":tables.append(rows)
    stages=[]
    days=[]
    year_match=re.search(r"\b(20\d{2})-\d{2}-\d{2}\b",markdown)
    year=int(year_match.group(1)) if year_match else 2000
    def dates(text):
        result=[]
        for match in re.finditer(r"(?<!\d)(?:(20\d{2})[-/])?(\d{1,2})[-/](\d{1,2})(?!\d)",text):
            try:result.append(date(int(match.group(1) or year),int(match.group(2)),int(match.group(3))))
            except ValueError:return []
        return result
    for table in tables:
        if not table:continue
        headers=table[0]
        if "日期" not in headers:continue
        date_index=headers.index("日期")
        if "阶段" in headers and "学习日" in headers:
            count_index=headers.index("学习日")
            for cells in table[1:]:
                if len(cells)<=max(count_index,date_index):continue
                count=re.fullmatch(r"(\d+)\s*(?:天|日)?",cells[count_index])
                period=dates(cells[date_index])
                if count and len(period) in {1,2}:
                    stages.append((cells[headers.index("阶段")],period[0],period[-1],int(count.group(1))))
        elif "日" in headers or "天" in headers:
            for cells in table[1:]:
                if len(cells)>date_index:
                    parsed=dates(cells[date_index])
                    if len(parsed)==1:days.append(parsed[0])
    if not stages or not days:return
    unique_days=set(days)
    for title,start,end,expected in stages:
        if end<start:continue
        actual=sum(start<=day<=end for day in unique_days)
        if actual!=expected:
            raise PlanCalendarMismatch(f"计划日期校验失败：{title}标为{expected}个学习日，逐日表实际为{actual}个。请修正后重新保存。")
    if sum(item[3] for item in stages)!=len(unique_days):
        raise PlanCalendarMismatch("计划日期校验失败：阶段学习日合计与逐日表不一致，请修正后重新保存。")
