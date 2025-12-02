# context_precision 口径决策（2026-10-28）

> 结论：**采用路线 A（官方 RAGAS CP 保持官方口径，目标 75–80；不宣称官方 CP ≥90）**。自定义 fact-aligned CP 也救不了 cross30，不作为"每个指标 ≥90"的官方替代。

## 证据

1. **官方 CP 读数**（r1，100 题）：
   - M1b：71.35%；P1b（N=3）：71.21%；P1c（N=1）：70.14%。
   - 分层：非 cross30 子集 89–97%（holdoutA 97.5%），cross30 仅 **25–29%**；整体被 cross30 30 题拖住。
2. **CP judge 的结构性严格**（m001 实跑）：
   - 单事实 reference：CP 0.25–0.37；两条事实合并 reference：0.33（同一输入重跑可出 0.0）。
   - judge 理由："上下文仅覆盖通稿负责人，未覆盖公告负责人，对完整答案支持不充分"——**单个 context 必须支持完整 reference**，多事实答案的事实分散在不同切片时 CP 结构性为 0。
3. **自定义 fact-aligned CP 也不达标**：
   - 实现 `evaluation/judges/fact_cp.py`（每个 expected_fact 单独跑官方 ContextPrecision，再按 case 平均；标 `r1-fact`，非官方聚合）。
   - cross30 前 20 题实测：mean **0.3008**、median **0.2083**（`results/fact_cp_m1d_pilot20.json`）。把 reference 拆成单事实后 CP 仍低，说明不是"多事实 reference 聚合"唯一原因；CP judge 对"部分支持/改写"本身很严。
4. 因此，**官方 CP 90 不可能靠检索/答案优化达到**；任何让 CP≥90 的做法都必须改评测单位（把同一会议/同一答案的多个切片合并成一个 context）、改 reference 为单事实、或改 judge prompt——都不再是官方 RAGAS CP，必须标 custom，且不能与官方读数混比。

## 决策（冻结）

- 官方四指标继续按 r1 报告；CP 的现实目标：**75–80**，并分 source set 报告（cross30 单列）。
- 自定义 `r1-fact` 只作为诊断/附录，不进入简历"官方 RAGAS"数字，不宣称 CP ≥90。
- 后续优化集中在 faith/AR/CR（P1/P2）；CP 不再用 floor/top_k/压缩等已否决机制硬刷。

## 复现

- 官方 CP：`results/m1b_ragas_run{1,2,3}.json`、`m1d_ragas_run{1,2,3}.json`、`m1e_ragas_run1.json`
- fact CP pilot：`results/fact_cp_m1d_pilot20.json`；脚本 `evaluation/judges/fact_cp.py`
