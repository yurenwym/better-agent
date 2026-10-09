# Research 快照回放比较

效果结论：**FAIL**；发布资格：false。
计划分母：8；胜/负/平/无效：0/5/1/2。
费用来源：fixture/simulated；baseline/candidate：None/None（null 为未知）。

| Case | 差值 | 判定 | baseline | candidate | 失败阶段/原因 |
|---|---:|---|---|---|---|
| 2f3e2fc13a01 | 0 | tie | insufficient_evidence | insufficient_evidence | distilling / insufficient_evidence |
| 86b27396733d | 0 | invalid | UNKNOWN | UNKNOWN | writing / response_lost_no_automatic_resume |
| b2d2ffdd8825 | -1 | baseline | success | rejected | partial / incomplete_delivery |
| 85e71d745ae0 | -1 | baseline | success | rejected | partial / incomplete_delivery |
| 059c1dcb594b | -1 | baseline | success | rejected | partial / incomplete_delivery |
| 82fbb169e798 | -1 | baseline | success | rejected | partial / incomplete_delivery |
| 68868165c1b0 | -1 | baseline | success | rejected | partial / incomplete_delivery |
| 7615b63b2c0b | -1 | invalid | rejected | invalid | finalizing / MISSING_FIXTURE |
