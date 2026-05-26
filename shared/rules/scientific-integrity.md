# Scientific Integrity (Non-Negotiable)

## No Data Leakage

- **NEVER use ground truth, eval set examples, or known FP/FN cases in prompts, few-shot examples, or threshold tuning**
- If examples are needed in a prompt, use synthetic ones or draw from a separate held-out dev set
- Ground truth exists to **measure** quality, not to **inform** the solution
- Embedding eval-set patterns into the system is overfitting — it inflates metrics without improving generalization
- Before proposing any change to a prompt or pipeline, ask: "Does this change use any data from the eval set?" → If yes, **STOP — this is data leakage**

## Mechanism Before Metric

- **Explain WHY a change should improve quality in general** before predicting its effect on metrics
- Propose the mechanism first ("this rule prevents X because Y"), then hypothesize the metric impact
- If you cannot articulate a general principle behind the change, it is likely overfitting to known cases
- Never propose a change whose only justification is "it fixes these N known failures" — that is teaching to the test
- A good change improves an entire **class** of cases; a bad change memorizes specific instances
