# Finance Tracker Product Spec

## Product promise

Finance Tracker is a trustworthy local daily driver for one person. Within 30 seconds it should
answer:

1. What needs attention?
2. What is safe to spend?
3. What happens before the next paycheck?
4. Where did money go, and can I correct the explanation?
5. Am I on track for budgets and goals?

The product is not complete when charts exist. It is complete when the user can move from a signal
to the underlying transactions, correct them, and see every dependent answer update coherently.

## Product principles

- **Trust before cleverness.** Show source, freshness, confidence, and Item scope for derived facts.
- **Action before decoration.** Every alert leads to a useful workflow; every chart supports drill-down.
- **Editable automation.** Classification, recurring detection, rules, and forecasts explain themselves
  and accept correction.
- **Money math is deterministic.** Store and calculate cents; use Chicago calendar dates; define
  pending, posted, transfer, refund, and recurrence behavior explicitly.
- **Local is a feature.** No cloud account, advertising, data resale, or production-data artifacts.
- **Progressive depth.** Overview stays calm. Details appear when the user asks for them.

## Product parity contract

Monarch is the primary broad-suite comparator. Copilot supplies the transaction-review and analysis
bar. YNAB supplies the planning and overspending bar. Rocket Money supplies the subscription bar.

Evidence informing this contract:

- [Monarch tracking](https://www.monarchmoney.com/features/recurring) combines searchable
  transactions, reports, recurring calendar/list views, investments, net worth, and customizable
  dashboard widgets.
- [Monarch rules](https://help.monarchmoney.com/hc/en-us/articles/360048393372-Transaction-rules)
  support rename/category/tag/hide/review actions, ordered rules, preview, retroactive application,
  and smart splits.
- [Copilot onboarding and core flows](https://help.copilot.money/en/articles/11157550-quick-start-guide)
  cover budgets, transaction review, cash flow, recurring schedules, goals, investments, and net worth.
- [Copilot splits](https://help.copilot.money/en/articles/5325255-splitting-transactions)
  preserve a parent total while allowing category, amount/percentage, type, and date edits.
- [Monarch rollovers](https://help.monarchmoney.com/hc/en-us/articles/4411119762196-Rollover-budget-feature)
  carry remaining category funds across months and distinguish fixed, non-monthly, and flex buckets.
- [YNAB targets](https://support.ynab.com/how-to-use-targets-rk5kkI9ks) expose underfunding,
  required contributions, progress, and funding adjustments.
- [YNAB overspending](https://support.ynab.com/en_us/overspending-in-ynab-a-guide-ryWoxEyi)
  turns an alert into a workflow for moving money from other categories.
- [Copilot recurring schedules](https://help.copilot.money/en/articles/3760068-creating-recurrings)
  support confirmed matching rules and weekly, biweekly, monthly, and non-monthly cadence.

Feature parity cannot be claimed until R1 and R2 in `IMPLEMENTATION_PLAN.md` are complete as usable
workflows. Read-only panels do not count.

## Information architecture

The hamburger sidebar has two groups.

### Views

- **Overview:** attention queue, safe-to-spend, spending pace, cash-flow risk, upcoming events.
- **Cash flow:** forecast timeline, income/spend history, scenario comparison, source confidence.
- **Spending:** transaction review, categories, merchants, search, filters, drill-down.
- **Bills and income:** recurring calendar/list, subscriptions, expected income, paid/missed state.
- **Planning:** budgets, goals, purchase decisions, overspending resolution.
- **Connection:** accounts, balances, freshness, sync history, repair, inclusion settings.

### Tools

- Date range
- Sync
- Transactions to review
- Purchase check
- Global transaction search (R1)
- Export (R2)

## R1 — Trustworthy daily driver

### 1. Daily overview

Entry: open `/dashboard`.

The first screen shows, in this order:

1. Blocking connection or stale-data issue.
2. Transactions needing review.
3. Safe-to-spend and next projected shortfall.
4. Spending pace versus plan.
5. Next bills/income and subscriptions worth attention.

Success: the user can resolve each item without hunting through another page. Dismissed/resolved
items disappear immediately and dependent figures refresh once.

### 2. Transaction workspace

Required workflow:

1. Search by merchant/original description, category, tag, account, type, amount, and date.
2. Open a transaction without losing filters or scroll position.
3. Edit display merchant, category, transaction type, notes, tags, review state, and exclusion state.
4. Split by exact cents or percentages; children must sum to the parent exactly.
5. Apply edits to one transaction or create an exact/contains rule with a preview count.
6. Bulk-review or bulk-edit a filtered selection.
7. Undo the latest edit/rule application from an audit record.

Data-quality behavior:

- Pending rows never enter spend/income totals.
- A posted replacement does not coexist in totals with its pending version.
- Internal transfers and investment liquidations remain visible but excluded from ordinary income/spend.
- Refunds can match an original expense without being mislabeled as income.
- Ambiguous classifications stay visible in the review queue; confidence is never fabricated.

### 3. Budget workspace

Budget modes:

- **Fixed:** known monthly commitment.
- **Flexible:** shared monthly discretionary pool.
- **Non-monthly:** sinking fund with rollover and optional due date.
- **Excluded:** visible in reports but outside the spending plan.

Each category shows planned, rollover, actual, remaining, pace, and status. The rollover contract is:

`previous remaining + current planned - current actual = current remaining`

Overspending is an action flow, not a red number. The user selects an overspent category, sees
eligible categories with available funds, moves an exact amount, previews the result, and confirms.

### 4. Recurring and subscriptions

Required workflow:

1. Suggested recurring series enter a confirmation queue.
2. User can edit name, cadence, expected amount/range, date window, category, and match rule.
3. Calendar and list views show bills, subscriptions, and income separately.
4. Each occurrence becomes upcoming, paid, missed, skipped, or manually resolved.
5. Subscription cards show monthly and annual cost plus change from prior occurrences.
6. Posted transactions link to exactly one expected occurrence; forecast does not re-add it.

### 5. Accounts and connection health

Account workspace shows institution, account name/type, masked identifier, included/excluded state,
current/available balance, last successful sync, data source, and any actionable Item error.

Repair preserves the existing Item. Manual sync reports added/modified/removed counts. Cached data is
clearly labeled and remains usable without pretending to be live.

### 6. R1 exit gate

R1 passes only when:

- A user can connect, sync, review/correct transactions, create a budget, confirm recurrings, and
  understand the next 30 days without direct database work.
- Every overview number drills into the rows that produced it.
- All mutations are Item-scoped, validated, reversible where practical, and covered by temp-DB tests.
- Synthetic normal/empty/error/stale states pass desktop and mobile rendering plus keyboard checks.
- No production credential, database, screenshot, or account value enters tests or git history.

## R2 — Planning and insight

- Filterable cash-flow, category, merchant, income, fee, and net-worth reports.
- Period comparison using complete-period rules and honest partial-period labels.
- Sankey-style money flow with transaction drill-down.
- Goals with amount/date, linked funding source, required contribution, pace, and pause/snooze.
- Net-worth history with manual assets/debts and inclusion controls.
- Saved report views and CSV export.
- Scenario planning across purchase amount/date, bill timing, and goal contributions.

## Differentiator: Decision Lab

Purchase Check grows into an explainable decision tool instead of a yes/no calculator.

Inputs:

- Purchase amount and date
- Payment account/category
- Optional recurring or one-time classification
- Optional goal to protect

Outputs compare baseline versus purchase scenario:

- Lowest balance and date
- End-of-week and next-payday balances
- Safe-to-spend change
- Budget/category impact
- Goal delay, if any
- Confidence, source, and missing evidence

If the scenario is risky or uncertain, offer deterministic alternatives:

- Earliest safer purchase date
- Maximum amount that preserves the configured buffer
- Category transfer needed to cover it
- Goal contribution tradeoff

The engine never says “affordable” with insufficient confidence. Every recommendation links to the
events and assumptions that produced it.

## Execution DAG after the current verification wave

1. **Transactions foundation:** query/search/detail model, review state, edit contract, audit record.
2. **Rules and splits:** depends on transaction detail and audit; preview before bulk application.
3. **Budget model/workspace:** depends on corrected posted transactions and categories.
4. **Recurring workspace:** depends on transaction matching; may run parallel with budget UI.
5. **Accounts workspace:** may run parallel after Item-scoping contracts are frozen.
6. **Overview integration:** depends on review, budget, recurring, forecast, and account-health contracts.
7. **R1 adversarial verification:** exact staged tree, synthetic visual states, accessibility, and data integrity.

Each implementation task gets one owner, explicit files, deterministic acceptance tests, and an
independent reviewer. Product slices should remain usable at every checkpoint.

## Deliberate non-goals

These do not block local core parity:

- Subscription-cancellation concierge or bill negotiation
- Credit-score/report resale features
- Custodial automatic savings transfers
- Household cloud sharing and cross-device cloud sync
- Native mobile apps
- AI-generated financial advice presented as fact

They may be reconsidered only after R1–R3 are trustworthy.
