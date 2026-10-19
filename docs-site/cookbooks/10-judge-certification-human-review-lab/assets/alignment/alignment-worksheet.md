# Alignment worksheet: the arithmetic, by hand

CALIBER computes judge-versus-human alignment in the product (Judges > Try it > **Human alignment**
> **Import labels** > **Check alignment**, or `POST /judges/{id}/alignment`). The product's job is to
run the judge on each labelled row. Everything after that is arithmetic over two columns of
pass/fail verdicts, and this worksheet shows it so you can check the readout yourself, or any other
judge's.

[`expected-alignment.csv`](expected-alignment.csv) holds both columns for the 50-row dataset:
`human_label` (the reviewer's verdict, from the dataset file) and `stand_in_strict_judge` (what the
lab's scripted stand-in model answers for `FaithfulnessJudge`). With a real model the judge column
is yours to read off the **Evaluations** per-row results or the alignment response's `per_example`.

## 1. Tally the 2x2

Treat the judge's *pass* as the positive class.

|                | human pass | human fail |
| -------------- | ---------- | ---------- |
| **judge pass** | TP = 23    | FP = 5     |
| **judge fail** | FN = 4     | TN = 18    |

- **FP** (judge pass, human fail): the judge waved through something a person rejected. These are
  the dangerous ones for a release gate: J03, J12, J19, J24, J33.
- **FN** (judge fail, human pass): the judge blocked something a person accepted: J14, J17, J22, J30.

## 2. Agreement and kappa

```text
n   = 23 + 5 + 18 + 4                      = 50
p_o = (TP + TN) / n  = (23 + 18) / 50      = 0.82            (observed agreement)

judge says pass:  (23 + 5) / 50 = 0.56     human says pass:  (23 + 4) / 50 = 0.54
judge says fail:  (4 + 18) / 50 = 0.44     human says fail:  (5 + 18) / 50 = 0.46

p_e = 0.56 × 0.54 + 0.44 × 0.46            = 0.3024 + 0.2024 = 0.5048   (agreement expected by chance)
kappa = (p_o - p_e) / (1 - p_e)            = (0.82 - 0.5048) / 0.4952   = 0.6365
```

The product reports `agreement_rate` 0.82 and `cohen_kappa` 0.6365 (the Judges page shows 82% and
0.64). Kappa matters more than agreement: a judge that says "pass" to everything would agree with a
mostly-passing sample and measure nothing.

## 3. The certification rule used in this lab

A judge is **certified** only when all three hold, otherwise it is **rejected** (or **not
certifiable** if the sample is too small to say):

| Check | Threshold | Why |
| --- | --- | --- |
| labelled rows scored | all of them, and at least 30 | a row the judge errored on is left out of the numbers; five rows can clear both bars by luck (below) |
| agreement | 0.80 or more | the lab's gate (`verification.yaml`) |
| Cohen's kappa | 0.60 or more | agreement alone is inflated by a skewed sample |

`FaithfulnessJudge` with the stand-in: 50 of 50 scored, agreement 0.82, kappa 0.6365: **certified**.

A weak judge, for contrast (`LenientJudge`, which passes anything that is not empty or cut off):
TP 27, FP 21, TN 2, FN 0. Agreement (27 + 2) / 50 = 0.58, kappa = (0.58 - 0.5368) / 0.4632 = 0.0933:
**rejected**. It never fails a wrong answer, so it agrees with the humans barely more than chance.

## 4. Why thirty rows

The first five rows (J01 to J05) give judge `[pass, fail, pass, fail, pass]` against human
`[pass, fail, fail, fail, pass]`: agreement 4/5 = 0.80 and kappa = (0.8 - 0.48) / 0.52 = 0.6154. Both
bars cleared on five rows, and the product returns those numbers without complaint. The 50-row
numbers are 0.82 and 0.6365, so here the small sample happened to land near the truth; it would not
always. Insist on a sample large enough to include the hard cases.

## 5. The ten rows of the UI path

The UI path has a person review ten traces (J01, J02, J03, J04, J05, J08, J09, J11, J14, J26):
TP 4, FP 1 (J03), TN 4, FN 1 (J14). p_o = 8/10 = 0.80, p_e = 0.5 × 0.5 + 0.5 × 0.5 = 0.50, kappa =
(0.80 - 0.50) / 0.50 = 0.60. The Judges page shows **Agreement 80%**, **Cohen's κ 0.60**, **FP 1 ·
FN 1**, over 10/10 examples. That meets both numeric bars but not the 30-row minimum: a readout, not
a certification. `LenientJudge` on the same ten rows: TP 5, FP 4, TN 1, FN 0, agreement 60%, kappa
0.20.

## 6. What to do with a disagreement

Every row with `agree = NO` is a calibration input, not noise:

1. Read the reviewer's reason (`human_label_reason` in the dataset file) and the judge's rationale.
2. Decide which side is wrong. A row the judge misses because its rubric never mentions it (J03,
   J12, J19, J24, J33: unsupported claims with no figures in them) is a rubric gap: reword the
   instructions. A row a person would accept but the judge fails on formatting (J14, J17, J22, J30:
   a derived or reformatted figure) is a rubric ambiguity: say whether derivations are allowed.
3. Add the row back to the test set as a hard case (`POST /eval-datasets/{id}/examples/from-trace`
   with `expected` overridden to the evidence, tag `judge-disagreement`) so every later run sees it.
4. Re-run the Evaluations scorecard and the alignment check. Do not certify until it clears.

One trap when you label through a Review Queue: **Import labels** passes every answer on the queue
item other than the pass/fail one to the judge as `{{ expectations }}`. Keep those answers to the
evidence. A reviewer's explanation that quotes the unsupported claim becomes "evidence" the judge
then agrees with: with the stand-in, putting `human_label_reason` into the item drops agreement
from 0.82 to 0.66 (FP 5 to 13) and kappa from 0.6365 to 0.2952.
