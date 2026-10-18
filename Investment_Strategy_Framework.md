# Investment Strategy — Framework (Canonical)
**Last updated:** September 27, 2026 (Tier 2 built; see the note at the end)
**Prior version:** September 25, 2026
**Pairs with:** `Investment_Ledger_Master_Aug28.md` (the positions/watchlist/actions record; the filename carries the snapshot date, so use the latest dated successor if one exists)
**Read by:** you, in Claude chats, and Sentinel's Tier 2 (Phase 7) as its rulebook. Any change to this file gets a numbered entry in `DECISIONS.md`.
**Investor context:** Canada (Quebec resident assumed), Wealthsimple Premium (USD accounts enabled — can hold USD natively in TFSA/RRSP/Non-Reg, no per-trade FX fee once enabled), fractional shares, small test sizing, ~2-year horizon, low turnover. Holds a **Nasdaq-100 (QQQ)** index fund and an **S&P 500** index fund (the latter recorded Sep 26, 2026) — also holds AAPL/NVDA/GOOGL/MSFT/NFLX directly in Non-Reg, flagged as uncompensated overlap per Operating Rule 2.
**Disclaimer:** Educational only. Not personalized financial or tax advice. Confirm tax treatment and order mechanics independently. Consult a licensed advisor before acting.

**Account mechanics notes (added Aug 8, 2026):**
- **TFSA withdrawal/room regeneration:** withdrawn amounts (including gains) are added back to contribution room, but only on **January 1 of the following calendar year** — not immediately. Re-contributing in the same calendar year before room regenerates risks an over-contribution penalty (1%/month on the excess).
- **USD contributions to TFSA/RRSP:** CRA requires contribution amounts in CAD, converted at the **exchange rate on the date of the contribution/transfer** (Bank of Canada daily rate). This conversion is for room-tracking purposes only — the actual cash/security held inside the account stays in USD via Wealthsimple's USD accounts feature; nothing is forcibly converted. The relevant date is the date money or a security **enters the account**, not the date it's used to buy a stock inside the account.
- **In-kind transfers from Non-Reg to TFSA/RRSP** (e.g., moving ASML/LBRT) trigger a **deemed disposition** for capital-gains purposes on the Non-Reg side as of the transfer date, separate from the room calculation above — a real tax event even though no cash changes hands.
- **RRSP withholding treaty exemption:** 0% US withholding on US-source dividends held in RRSP (Canada-US treaty carve-out). TFSA gets **no equivalent exemption** — US dividends in TFSA still lose 15% permanently, with no foreign tax credit available (unlike Non-Reg, where the credit is generally recoverable). This is the basis for routing US dividend payers (e.g., LBRT) to RRSP rather than TFSA. Note: the RRSP exemption is US-treaty-specific — it does not extend to other countries (e.g., ASML/Netherlands gets no equivalent RRSP carve-out).
- **Account priority when room is available:** prefer TFSA/RRSP over Non-Reg whenever unused room exists — Non-Reg gains and dividends are taxed, registered-account growth isn't. Between TFSA and RRSP: RRSP for US dividend payers (withholding exemption); TFSA for Canadian names and non-dividend growth stocks (full withdrawal flexibility, regenerating room). RRSP contributions also generate a current-year tax deduction, valuable if current marginal bracket exceeds expected retirement bracket — a personal-circumstance question, not a structural rule.

---

## 0. Core philosophy

Run **three sleeves** that compete for each dollar through **one shared scorecard**. Each sleeve is a distinct engine and must not be judged by another sleeve's rules:

- **Sleeve 1 — Dislocations (value):** buy *hated quality at a discount*, wait for mean reversion to a known fair value. Risk control = the discount. Exit = take-profit at target, or thesis-break.
- **Sleeve 1B — Value Gap + Corporate Catalyst:** buy companies trading at a *measurable discount to intrinsic value* where management has announced a **specific, time-bound corporate action** to close it (buyback, spin-off, acquisition, restructuring). Same scorecard as Sleeve 1. The catalyst must be internal and dated — "we'll create value over time" without a hard date is a value trap, not Sleeve 1B.
- **Sleeve 2 — Emerging Growth:** buy *fundamentals inflecting up with a long runway*, even after a run. Risk control = small size + proof-milestone exit. Let winners run (no price cap).

**The cardinal error to avoid:** applying Sleeve 1's "must be cheap" test to Sleeve 2. Growth bets are *supposed* to look expensive and to have already risen. Reject them only for a real veto (below), not for being up.

---

## 1. Shared scorecard (every candidate, any sleeve)

Score 1–5 each; back the higher total per available dollar:
1. **Asymmetry** — upside if right vs. downside if wrong.
2. **Catalyst proximity & concreteness** — a dated/identifiable trigger beats open-ended hope.
3. **Monitorable proof** — the *leading indicator* that shows it's working before price does (bookings, margin, RPO, contract conversion, units). No measurable proof = uninvestable (can't be monitored or exited).
4. **Defined exit** — the pre-written thesis-break / proof-break.

**Scores must be recorded as numbers, not implied by prose (added Sep 25, 2026).** Every name that reaches Watch or Held status gets its four scores written down (e.g. `3-4-5-4 = 16/20`), not just a qualitative verdict. This was previously optional and, in practice, was never done — every Watch/Held entry in the ledger has a reasoned writeup but no recorded total, which makes "back the higher total per available dollar" impossible to actually apply. Untracked historically: **it does not need to be reconstructed retroactively for existing positions** — start recording going forward.

**Head-to-head comparison required when capital is contested (added Sep 25, 2026).** If two or more Watch names are realistic candidates for the same available cash, score both explicitly and present them side by side before choosing — don't decide on narrative persuasiveness alone. If only one name is actionable at the time (the usual case), a solo score is enough; the comparison table is only mandatory when there's a real choice to make.

---

## 1A. Pre-entry diligence checks (mandatory, added Jul 26, 2026)

Run **both** of these before any entry decision — not just at initial screening, but re-run at every proof-checkpoint (hold/add/exit decisions too). They catch things the four-factor scorecard misses on its own.

**1. Insider selling — classify before reacting.**
- Pull the most recent Form 4 filings. If sales are executed under a **pre-arranged Rule 10b5-1 plan** — especially "sell to cover" transactions tied to RSU/PRSU vesting, on a plan adopted months before the sale, as part of a regular recurring cadence — this is routine. Note it, don't flag it as a signal.
- The actual red flag is **discretionary, non-10b5-1 selling that clusters right before or during a decline**, especially from multiple insiders at once, or a newly-adopted plan timed suspiciously close to material news. That pattern is a real deduction against confidence in the thesis.
- Never treat "insiders have been selling" as a standalone bearish (or bullish) data point — always check the plan type and adoption date first.
- **Count distinct insiders, not transactions (added Sep 26, 2026).** One executive buying in ten lots is one opinion, not ten. Report insider buying as "N insiders, $X total, latest date." A single serial buyer is weaker evidence than several insiders buying independently after the same decline. *Corrected Sep 27, 2026:* this rule was first illustrated with AMRZ as "almost all one buyer, the CEO". That was wrong: the SEC filings show at least seven executives buying in Aug–Sep 2026 alongside the CEO (Tier 2 caught it). AMRZ is therefore an example of broad buying, not narrow.
- **Ignore trivial purchases (added Sep 27, 2026).** A handful of shares worth a few hundred dollars (typically a reinvested dividend or plan top-up) is not a conviction signal and doesn't count toward the number of buyers. Precedent: an AMRZ director's 5.47-share "purchase" (about $244) on Aug 27, 2026.

**2. Full-year price history, not just the screen-window number.**
- Every "X% off 52-week high" or "X% down YTD" stat gets checked against **at least 12 months of price history**, not whatever window the news article or screener happens to quote.
- Determine specifically: **is this a single, dateable event (one bad print, one macro shock) or a chronic, multi-quarter grind?** A one-time air-pocket is a genuine Sleeve 1 candidate. A steady month-over-month decline stretching back a year or more is closer to the BSX/ZTS/IFC.TO "pattern, not misread" disqualifier (§4B Research Log precedent) — even if the most recent print looks clean in isolation.
- Cross-check against **2–3 direct peers over the same window.** If the stock fell meaningfully more than peers facing the same macro/sector conditions, the story is likely idiosyncratic rather than sector-wide — treat "soft macro" explanations skeptically until peer comps actually confirm them.
- Where available, compare the multi-year **EPS growth trend vs. share price trend.** Earnings compounding while the stock re-rates down for multiple consecutive quarters (persistent de-rating) is a genuine warning sign distinct from "quality temporarily hated," and deserves the same scrutiny as a pattern-based disqualifier — not just treated as a bigger discount to buy.

**3. Short-seller fraud/accounting allegations — automatic disqualifier until independently resolved (added Jul 28, 2026).**
- Not every bear-case report is the same category of risk. A short-seller report questioning **valuation, competitive positioning, or growth durability** (e.g. the Hunterbrook note on HLNE) is ordinary bear-case material — run it through the normal scorecard.
- A short-seller report alleging the **reported financial statements themselves are false** — inflated EBITDA/revenue, undisclosed debt, off-balance-sheet structures, accounting fraud — is a different category entirely, and is an **automatic disqualifier** regardless of how attractive everything else looks (discount to target, insider buying, headline growth numbers), because those very numbers are what's in dispute.
- "Down a lot + insider buying + strong reported growth" is not sufficient to override this. A CEO buying shares personally is a genuinely positive signal, but it is not a substitute for independent verification.
- **Re-add condition:** an independent audit confirmation, formal regulatory clearance, or a genuine line-by-line rebuttal of the specific figures in dispute — not a general "misleading and inaccurate" denial with no itemized response. A company that stops answering specific follow-up questions from the short-seller (rather than answering them) is a bad sign, not a neutral one.
- This is a hard rule, not a scorecard deduction — do not average it against other positives.
- **What does NOT trigger it by itself (added Sep 27, 2026): securities class actions and law-firm "investigations".** Plaintiffs' firms file these after almost every large drop, often alleging the numbers were misleading. On their own they are ordinary bear-case material: note them, don't disqualify. They trigger this rule only when backed by something independent: a named short-seller or forensic-accounting report, a restatement, an SEC or other regulator action, or an auditor resignation or qualified opinion. Precedent: BMI (Sep 2026), where Tier 2 treated a class action alone as disqualifying; BMI was correctly dismissed anyway, on its pattern of repeated misses (check 2).

**Net effect:** §6A's qualitative pass (ticker + bear-case check) now includes all three checks above before a name can be scored as a live Sleeve 1/1B/2 candidate.

---

## 1B. Default exit target (added Sep 25, 2026)

Applies to **Sleeve 1 and Sleeve 1B only** — Sleeve 2 keeps its no-price-cap, proof-break-only exit (§5) unchanged.

**+20% above cost is the floor, not the target.** Set the GTC take-profit at the higher of (a) cost + 20%, or (b) a specific, sourced analyst-consensus or intrinsic-value target — never below cost + 20% just because consensus happens to be lower. A consensus target above +20% can raise the exit; it never lowers it below the floor.

*Why this was added:* current targets vary widely for no stated reason — LBRT's is ~23% above cost, ATRL ~26%, CJT's stretch ~37%, ASML ~18% — each individually sourced from a different consensus figure with no consistent floor underneath. That's fine as an upper bound (a well-sourced 37% target is better than an arbitrary one), but nothing currently stops a future entry from getting a sub-20% target just because that's where consensus happens to sit. The floor doesn't replace the diligence behind each target, it sets a minimum bar under it.

**Scope (added Sep 26, 2026, confirmed by you):** the floor binds every new entry and every target set or reset from Sep 26, 2026 onward. Existing GTC orders are not force-reset; each is checked against the floor at the position's next proof-checkpoint (normally its next earnings print, per §6B step 3). As of Sep 26, these existing exits sit below cost + 20% (each applies only if the name is Sleeve 1/1B — sleeve is not yet recorded in `positions.yaml`): ATRL GTC $100 (+17.7%), EFN GTC $30 (+8.7%), ALB GTC $155 (+18.3%), MELI GTC $2,230 (+12.6%), DUOL GTC $170 (+17.6%), ASML target $1,999.96 (+17.9%, no GTC possible).

---

## 2. Account placement (recommendations)

| Account | Best for | Names tend to be |
|---|---|---|
| **TFSA** | Canadian growth / low-or-no-dividend; tax-free compounding | Most Sleeve-2 growth + Cdn dislocations + EQB |
| **RRSP** | US-listed names, esp. dividend payers (treaty shields 15% withholding) | US names (VISA, LMT, BSTZ, MDT, FDX, FISV) |
| **Non-Reg** | Canadian eligible-dividend income (dividend tax credit) | CNQ, FFH.PR |

Canada has **no short-term/long-term capital-gains distinction** (50% inclusion either way) — so there is no tax reason to cap holds at a year. Low turnover also avoids CRA "carrying on a business" reassessment risk (the *Ahamed* TFSA precedent) — keep trading infrequent.

**RRSP tax note:** transactions inside the RRSP generate zero capital gains events — buy and sell freely. Tax only occurs on withdrawal, when the full amount is treated as ordinary income. The Canada-US tax treaty exempts RRSP accounts from the standard 15% withholding on US dividends — this is why US dividend payers belong in the RRSP, not the TFSA.

**Non-US foreign domicile caveat (added Aug 4, 2026):** the RRSP withholding shield above is a **Canada-US treaty provision specifically** — it does not automatically extend to non-US-domiciled companies just because they trade on a US exchange. A stock like ASML (Dutch-domiciled, NASDAQ-listed) is governed by the **Canada-Netherlands treaty** for dividend withholding, which is a separate question requiring separate verification — don't assume "US-exchange-listed" is the same test as "US-domiciled" when recommending RRSP placement. When domicile is non-US, default to TFSA (no worse than Non-Reg, and avoids assuming an unconfirmed shelter) until the specific treaty treatment is verified with the broker.

**Execution discipline (added Aug 4, 2026):** account-recommendation logic is only useful if it's followed at the moment of order entry. This session, two entries (ASML, LBRT) were both recommended for TFSA/RRSP but executed in Non-Reg — for LBRT this forwent a clean, available RRSP withholding shield; for ASML the Non-Reg placement was arguably the safer default given the unresolved Dutch-treaty question, but wasn't a deliberate choice either time. **Before submitting an order, confirm the account matches the recommendation** — the same discipline as verifying prices/dates before acting, just applied to the order ticket itself rather than the research.

**Small-position TFSA override for US dividend payers (added Aug 9, 2026):** the RRSP-for-US-dividend-payers rule above assumes the withholding shield is worth structuring around. Below roughly **$1-2K position size**, it usually isn't. A US dividend payer yielding ~1-1.5% held at $1,000 generates ~$10-15/year in dividends; the 15% RRSP-avoided withholding on that is only **~$1.50-2.25/year** — trivial against two real costs of using RRSP instead: (1) RRSP contribution room is scarcer than TFSA room (no regeneration mechanic, capped at 18% of earned income) and is better spent on positions where the shield is worth real money; (2) RRSP withdrawals are taxed as full ordinary income on the entire amount (principal + gains), which cuts against positions explicitly sized as tactical/test entries with a defined near-term GTC exit — TFSA's tax-free-in-tax-free-out mechanic fits that intent better, even accounting for the regeneration delay to Jan 1 of the following year. **Rule of thumb:** below ~$1-2K, default US dividend payers to TFSA over RRSP; above that threshold, the standard RRSP recommendation resumes applying as the position's dollar exposure to withholding becomes material. This assumes adequate TFSA room is available — if TFSA room is tight and RRSP room abundant, that personal-circumstance constraint can override the threshold logic in either direction, same as the existing RRSP-deduction-value caveat in the base account-priority rule above.

---

## 3. Portfolio constraints (hard rules)

- Max single position ~8% of total; correlated names count together.
- **Combined cap on a shared named risk (added Sep 25, 2026): ~15% of total** (roughly 2x the single-position cap) for any group of positions flagged as sharing the same structural risk — e.g. the CRWV/NBIS/IREN "AI-infra leverage and dilution" bucket. "Correlated names count together" above was never given a number; this is that number. Adjust if 15% doesn't match your actual risk tolerance — it's a starting point, not a figure derived from anything in the ledger.
- **The 8% single-position cap does not force a trim on organic growth (resolved Sep 25, 2026).** A Sleeve 2 winner that grows past 8% purely through price appreciation is not sold down for size alone — that would contradict "let winners run." Instead, a position that organically exceeds **15% of total portfolio value** (not 8%) triggers a mandatory concentration *review*, not an automatic sell: re-run the scorecard and the proof-milestone check at that size, same as any other proof-checkpoint, and decide from there. The 8% cap still applies in full to new money going into a position, at entry or on an add.
- Cash/dry powder 10–15% at all times.
- Sleeve-2 names sized **smaller** than Sleeve-1 and Sleeve-1B; pre-profit names smallest.
- **Index-overlap presumption (changed from exclusion on Aug 22, 2026 in the ledger; written into this framework Sep 26, 2026):** for the **current top 15 Nasdaq-100 constituents by QQQ index weight** (verify on Invesco's QQQ holdings page at decision time — the list drifts with rebalances), individual-stock consideration is **not blocked**, but carries a standing presumption that a position just re-buys index exposure at market weight. The presumption must be explicitly overridden by a **real, named, dateable catalyst** — the bar used for SPGI, ICE, RDDT and META — before the name is scored; familiarity is not a reason to skip it. This replaces the old "≥20% dislocation only" rule. **Scope, confirmed by you Sep 26, 2026: the top-15 QQQ names AND every S&P 500 constituent**, because you hold both index funds (this replaces the ledger's undefined "S&P 500 where relevant"). Accepted side effect: most hits from the Large-cap screen (§6A) are S&P 500 members, so they need a named, dateable catalyst before scoring — for large caps, pure Sleeve 1 dislocations without a catalyst will rarely qualify. Names in neither index are not gated by this rule.
  All **other** Nasdaq-100 constituents (~85 names, individually <~1% of QQQ) compete on the normal scorecard with no special bar — **unless they are also S&P 500 members**, in which case the presumption above applies.
  *Rationale:* the original rule's purpose was to prevent unintentional concentration in names you already hold meaningful indirect exposure to via the index. The top ~10–15 names carry roughly 50–65% of QQQ's weight; the remaining ~85 carry the rest, where doubling exposure via a direct position is immaterial.

---

## 4. Sleeve 1 — Dislocation rules

- **Entry gate:** sentiment/macro/sector-driven selloff · analyst Buy consensus · genuine price-vs-value gap · articulable reason the bear case is overstated · FCF-positive or credible path.
- **Exit:** never sell on a price drop — only on thesis-break **or** a pre-set **GTC take-profit limit** at the upper target band (the "clean exit with gains"). Wealthsimple resting limits generally need whole shares; fractional remainders need a manual sell.
- **Dead-money rule:** if the catalyst hasn't arrived in ~24 months, reassess and recycle.
- **Data hygiene:** every "hard date" must cite a primary source and be re-verified before acting; refresh prices before any deployment (the "HON Jun 29" error lesson).

---

## 4B. Sleeve 1B — Value Gap + Corporate Catalyst rules

- **Entry gate:** measurable discount to intrinsic value (P/B, EV/FCF, sum-of-parts, DCF) + specific announced corporate action with a **hard date** (buyback program, spin-off, acquisition close, asset sale, restructuring milestone). Both conditions required simultaneously.
- **Disqualifier:** vague capital allocation language without a dated action = value trap. Do not enter.
- **Exit:** same as Sleeve 1 — thesis-break OR pre-set GTC take-profit limit. No downside stops on price alone.
- **Proof to monitor:** the corporate action progress (integration milestones, operating ratio, margin recovery, buyback pace) is the leading indicator. If the action stalls or reverses, that is the thesis-break.
- **Dead-money rule:** same 24-month rule applies.
- **Sizing:** similar to Sleeve 1, but conservative — these are often mid-recovery stories. Don't size up until first proof checkpoint confirms.

---

## 5. Sleeve 2 — Emerging-growth rules

- **Buy strength** when fundamentals inflect with a multi-year runway; "already up" is not a veto.
- **Risk control = small size + proof-milestone exit** (sell when the leading indicator breaks, regardless of price). No take-profit cap. Organic growth past the 8% single-position cap is not a sell signal by itself — see §3's resolved cap rule (review, not auto-trim, past 15%).
- **Two real vetoes:** (a) index overlap, unless the §3 presumption is overridden by a named, dateable catalyst; (b) a risk you've explicitly disqualified elsewhere (e.g., subprime credit cycle → the goeasy/GSY rule).
- Each name records: risk tag · catalyst type · leading indicator · exit trigger.

---

## 6. Operating cadence (how to run it)

The strategy is low-turnover with quarterly catalysts, so **daily *discovery* is the wrong cadence — it manufactures noise and tempts overtrading.** Split it:

- **Daily, automated (no effort — Sentinel, since Sep 20, 2026; updated Sep 26):** closing prices for tracked US names, the earnings/decision calendar page, and Tier 1's mechanical escalation rules (a 15% one-day move or 10% earnings surprise on a tracked name; a screen candidate clearing dislocation + conviction). Sentinel watches, it never trades: resting GTC orders and any broker price alerts still do their own job. Canadian tickers have no automated price source yet, so broker alerts are their only daily coverage.
- **Weekly (~15 min):** open the calendar page, clear the Tier 1 queue (every escalation is marked done or dismissed, with a reason), glance at news on held names. The screen, Tier 1 and Tier 2 run automatically every Saturday morning (§6A, §10), so reviews are ready for the weekend and any orders can be placed before Monday's open (changed from Sunday on Sep 27, 2026).
- **Event-driven / monthly (deeper):** a Claude session using the prompt in §7 — refresh prices, recompute P&L, flag catalysts within 4 weeks, check proof-milestones, and **screen for one or two new candidates** per the scorecard.
- **Quarterly:** earnings checkpoints on every held/watch name (the real decision points).

---

## 6B. Post-earnings reaction protocol (added Aug 7, 2026)

Four names this cycle (HLNE, SPOT, EFN, ATRL) reported strong headline growth, and in three of the four cases the stock's reaction diverged from the headline — sometimes rallying through a beat (HLNE), sometimes fading a beat within 24-48 hours (EFN, ATRL), sometimes falling cleanly on a real miss (SPOT). Headline growth alone was never a reliable predictor of the reaction, and the reaction alone was never a reliable verdict on the thesis. Three checks, in order, before updating a position's status after any earnings print:

1. **Check the specific pre-defined condition, not the headline.** Every held/watch name has a *stated* gate (e.g., EFN's originations recovery, ATRL's MONARK timeline, HLNE's AUM/fee growth, SPOT's spend moderation) — evaluate the print against that specific gate first. A strong headline (revenue +X%, EPS +Y%) can coexist with the actual gate condition being unmet, ambiguous, or unrelated to what moved the stock.
2. **Don't treat day-of price action as the final verdict.** Pops and drops on the print date can reverse within 24-48 hours as the market digests specifics past the headline (EFN and ATRL both popped, then faded once the details came out — the pop was the less informative signal). Wait at least one full trading day before concluding a beat "held" or a drop is "real."
3. **Re-verify GTC exit targets against fresh post-print analyst consensus before assuming they need to move.** A target set weeks or months earlier may already be conservative relative to updated Street numbers (ATRL's $107/$115 turned out to match or undercut fresh post-print targets) — don't adjust a target based on price action alone without checking whether analysts have actually moved.

None of this overrides §1A's disqualifiers (fraud allegations, guidance cuts, etc.) — those remain immediate, regardless of price action. This protocol is for the more common case: a genuinely mixed or ambiguous print where the instinct is to react to the stock chart rather than the specific number the position was waiting on.

---

## 6C. Watch-list staleness (added Sep 25, 2026)

No hard cap on the number of active Watch names. Instead: a Watch name is **auto-parked to Dormant** (not Removed — no re-add penalty) when both are true:
- **No new information has been logged for it in 6+ consecutive weeks**, and
- **It has no dated gate falling within the next 8 weeks.**

This targets the actual failure mode on file: the Watch list grew to ~20 names with no mechanism to shrink, an 11-name diligence session (Aug 22) produced two explicitly incomplete entries (CELH, GPOR) from running long, and volume has been growing faster than anything retires it. Auto-parking isn't a verdict — a parked name can be re-opened the moment new information or a gate appears, same as any Dormant name today. It just stops a name with nothing new to say from consuming review attention every session.

**Practical effect:** at the start of any diligence session, check each Watch name's last-logged date and next gate date before re-reviewing it; park anything that clears both conditions above before spending time on the rest.

---

## 6A. Discovery mechanism — finding new candidates without manufacturing noise

**The problem:** Claude's web search is hypothesis-driven, not a market scan — it surfaces whatever financial media happened to cover, which skews toward mega-caps and headline stories. It cannot answer "show me every stock down 25%+ with a Buy consensus and positive margins" — that's a database query against the full market, which Claude doesn't have.

**The fix — two stages, division of labour:**

1. **Mechanical pre-filter (automated by Sentinel — updated Sep 26, 2026):** Sentinel's weekly job (Saturday 09:30 ET; Sunday 09:00 until Sep 27, 2026) runs the Finviz screen; Tier 1 then queues any candidate that shows a dislocation signal **and** a conviction signal (thresholds in `sentinel_tier1_rules.yaml`). You no longer run it by hand. **The live filter set is the one coded in `sentinel_scan.py`. If it ever differs from the table below, the code is what actually runs — reconcile the two rather than trusting either.** Last manually verified set (ledger §9, from a live Finviz screenshot, Aug 22, 2026; supersedes the Jul 26/28 table previously here):

   | Filter | Value |
   |---|---|
   | Market Cap | **Large ($10bln+)**; the same filters are also run as a **Mid ($2–10bln)** variant |
   | Debt/Equity | **Under 1** |
   | Operating Margin | **Positive (>0%)** |
   | Price/Free Cash Flow | **Under 40** |
   | Analyst Recom. | **Buy or better** |
   | Average Volume | **Over 300K** |
   | Performance | **YTD -20%** — the recency/dislocation filter; swap to Month -10% or Week for one-off short-window cuts |
   | Insider Transactions | **Positive (>0%)** |

   *History:* the Jul 26–28 version described Mid-cap only, 50-Day High/Low 30%+ below, Performance Quarter -20% and P/FCF Under 30; the Aug 22 screenshot showed the saved screen did not match that description. The reasoning behind P/FCF and Debt/Equity is unchanged: any "Under X" P/FCF bucket implicitly requires positive FCF, which maps to the Sleeve 1 "FCF-positive" gate. This is a **Sleeve 1 pipeline**; Sleeve 1B and Sleeve 2 names mostly still arrive through direct research (DUOL, UBER — ledger §9).

   **Relative Volume (>2) — manual toggle only, NOT part of the saved screen.** Confirmed empirically: stacking this onto the other 8 conditions returned zero results on a quiet day. Relative Volume is a same-day condition (today's volume vs. recent average), not a durable stock characteristic — only useful when actively checking "what's moving hard *today*" (this is how ASML, LBRT, and SE were originally found via news search, not this screen). Add it in manually for that specific use case; leave it off for the routine weekly pass.

   **Out of scope — BDCs and other credit vehicles (added Sep 26, 2026):** business development companies and similar lenders whose value is a NAV of marked loans are passed automatically, not scored. The screen's margin, P/FCF and Debt/Equity filters are not meaningful for them, and this framework has no rules for judging loan-book quality or dividend coverage. Precedent: OTF, dismissed Sep 26, 2026. A deliberate NAV-discount trade (the CEF/BSTZ alert idea) is a separate, manually-sourced decision, not a screen candidate.

2. **Qualitative pass (Claude, or Tier 2 once built):** the input is Tier 1's escalation queue (`python3 sentinel_tier1.py --show`), or a pasted ticker list for names found outside the screen. Claude does the actual work the screener can't: reads the "why is it down" story, checks whether the bear case is overstated or confirmed in the numbers, runs the shared scorecard (§1) plus the pre-entry diligence checks (§1A — 10b5-1 classification, 12-month price history + peer comparison), and — critically — **passes are passes**. A name that's down a lot but where the decline reflects real, ongoing deterioration (a *pattern* of guidance cuts, negative organic growth, repeated safety corrections, or chronic multi-quarter decline vs. peers) is not a candidate just because it's cheap. Log passes with a specific re-add condition so they aren't re-researched next month.

**What this is not:** a reason to look more often. The weekly screen is a 2-minute glance; if nothing on it looks interesting, that's a valid "nothing this week," consistent with the cadence above. The screen *replaces* ad-hoc "is there anything out there?" requests — it doesn't add a new always-on input to feel FOMO about.

**Standing instruction for Claude (updated Sep 26, 2026):** whenever this framework file is loaded at the start of a session, proactively ask for the output of `python3 sentinel_tier1.py --show` (open escalations) and `python3 sentinel_pipeline.py status` — don't wait to be asked. The screen now runs automatically, so the open queue, not a manual screen, is what needs attention on cadence.

---

## 7. Session bootstrap prompt (paste at the start of each Claude chat)

> You are my investment research partner. Load the two attached files: `Investment_Strategy_Framework.md` (this framework) and `Investment_Ledger_Master_Aug28.md` (positions/watchlist/actions). Treat the ledger as the source of truth for what I hold and my trigger levels.
>
> Today's tasks, in order:
> 1. Note time elapsed since the ledger's "Updated" date.
> 2. **Ask me to paste `python3 sentinel_tier1.py --show` and `python3 sentinel_pipeline.py status`** — the screen runs automatically (§6A); the open escalation queue is the input.
> 3. Web-search **current prices** for all held + watchlist names (all three sleeves) and recompute P&L vs. my cost.
> 4. Flag any **catalyst/earnings within 4 weeks**, and any **trigger level hit** (price alerts, NAV discounts, par).
> 5. For each held name, check its **proof-milestone / thesis** — flag any that look broken.
> 6. Screen for **1–2 NEW candidates** that fit the scorecard (Sleeve 1 dislocation · Sleeve 1B value gap + corporate catalyst · Sleeve 2 emerging growth), applying the §1A pre-entry diligence checks (10b5-1 classification, 12-month price history + peer comparison) before scoring any of them. Apply the §3 index presumption, and exclude the disqualified-risk buckets and out-of-scope vehicles (§6A). Prefer TSX/TFSA-eligible for S1/S2; US names go to RRSP.
> 7. Present everything as a concise table. Be a sparring partner — challenge weak picks; verify prices, don't trust my stale numbers.
> 8. **Do not edit either file unless I explicitly say so.** Educational analysis only; remind me it's not advice.
> Then ask me what I want to work on.

---

## 8. Dashboard generation prompt (paste when regenerating the HTML dashboard)

> Load the two attached files: `Investment_Strategy_Framework.md` and `Investment_Ledger_Master_Aug28.md`.
>
> Regenerate the Investment Dashboard HTML file using the following design language:
>
> **Design tokens (non-negotiable, updated Aug 8, 2026 — light theme):**
> - Background: `#ffffff` · Surface: `#ffffff` · Surface2: `#f4f5f7`
> - Border: `#dfe2e7`, `#c8ccd3`
> - Text: `#1a1d21` · Muted: `#65707d`
> - Functional colors ONLY — every color must carry meaning, never decorative:
>   - Green `#1a7a4c` = gain / actionable buy level / held status
>   - Red `#b23b3b` = loss / removed status
>   - Amber `#a8631f` = watch / caution / exit target price
>   - Blue `#2563a8` = upside-to-target figures, active nav tab, filled scorecard pips
>   - Neutral gray (`#eef0f3` bg / `#3a4453` text) = category badges with NO positive/negative signal — sleeve labels (S1/S1B/S2), account type (TFSA/RRSP/Non-Reg). These are informational, not good-or-bad, and must never use green/red/amber.
> - Table header background: `#eef1f5`, text `#3a4453`, border-bottom `2px solid #c8ccd3`.
> - Fonts: DM Mono (body/tables) + Fraunces (headers/values) via Google Fonts
>
> **Critical table header rule:** `thead th` must use the light thead background above, never the same color as the row surface — it must stay visually distinct from the body rows.
>
> **Critical price/analysis column rule:** every Held / Sleeve 1 / Sleeve 1B position table uses this column set, in this order:
> 1. **Entry Price** — plain text (what was actually paid, from trade confirmation where available)
> 2. **Current Price** — bold/emphasized text (most recently verified, with the check date)
> 3. **Gain % (vs Entry)** — green if positive, red if negative
> 4. **Analyst Consensus Target** — plain text, sourced live per name (e.g., "~$115–120, Street consensus post-print"). Note in-table that this is sentiment context, not a real exit level, since consensus targets are structurally biased optimistic (analysts rarely set targets below the current price).
> 5. **Price to Exit (GTC)** — amber text (the pre-written take-profit limit for S1/S1B; "Proof-break only — no GTC" for S2)
> 6. **Upside % (to Target)** — blue text, measured to the primary/conservative target where a range exists
>
> Ready-to-enter / not-yet-held names keep a **Price to Buy** column (green) instead of Entry Price / Gain %, since there's no entry yet.
>
> **Tabs to generate:**
> 1. **Action List** — all names, grouped by: Time-Sensitive · Held (full column set above) · Ready to Enter · Watchlist-Catalyst-Gated · Parked · Removed. Alert strips at top for urgent entries and thesis-work reminders.
> 2. **Sleeve 1 — Dislocations** — full detail: shares, entry price, current price, gain %, analyst consensus target, price to exit, upside %, thesis, exit-if.
> 3. **Sleeve 1B — Value Gap** — scorecard pip bars (10 pips, blue filled), corporate action, valuation gap, proof to watch, exit-if, urgency badges. Closed positions show realized gain, not live P&L.
> 4. **Sleeve 2 — Growth** — tier (HIGH/SOLID/THEMATIC), risk tag badges, proof to watch, proof-break exit. Price to Exit = "Proof-break only — no GTC".
> 5. **Catalyst Calendar** — two-column CSS grid, chronological, urgency colors (green = resolved/<4 weeks, amber = 1–3 months, muted = further out or mechanical).
> 6. **Exit Mechanics** — price-to-exit + thesis-break conditions + dead-money dates + operating rules table.
>
> **Badge set (functional colors per rule above):** S1/S1B/S2 (neutral gray) · TFSA/RRSP/Non-Reg (neutral gray) · Held (green) · Watch (amber) · Parked (gray) · Removed (red) · Urgent (green).
>
> All data from loaded files only — do not invent figures. Every price/target must trace to a verified chart, order confirmation, or dated search result in the source files — flag anything stale or unconfirmed rather than presenting it as current. Single self-contained HTML file. Standard disclaimer bar at bottom noting the data source and that this is educational only, not advice.
>
> For detailed CSS, badge code, and tab templates, load `Dashboard_Regeneration_Template.md` if available; otherwise derive directly from the tokens and rules above.

---

## 9. Performance benchmark (added Sep 25, 2026)

**The gap this closes:** nothing in this framework or the ledger currently compares the result of all this diligence against just holding the QQQ you already own. Individual closed-position returns are recorded (EQB +23.46%, OLLI +11.43%, MDT +9.5%), but never against what QQQ did over the same holding window — so there has been no way to tell whether the process is beating the index it's built around avoiding overlap with.

**Per position, at every ledger refresh, record:**
- QQQ's price on the position's entry date (source once, at entry; doesn't change).
- QQQ's current price (refreshed alongside the position's own current price).
- The position's own return vs. QQQ's return over the identical window, side by side.

Example row format for the Held Positions table (new columns, appended to the existing set):
`| ... | Position Return | QQQ Entry | QQQ Now | QQQ Return | Alpha |`

**Portfolio-level, at every ledger refresh:** a single dollar-weighted line — sum(each position's dollars deployed × its own return) vs. sum(same dollars × QQQ's return over that position's own window) — so positions entered on different dates are each compared to QQQ over their own holding period, not a single blended window that would misstate it.

**Closed positions keep their QQQ comparison permanently** in the Closed Positions table (§5 of the ledger) — this is the actual track record and shouldn't be dropped once a position exits.

*This is a reporting rule, not a decision rule* — it doesn't change entry or exit logic, it makes the existing logic's results checkable. It also gives Tier 2 (the planned automated analysis layer) real calibration data that doesn't currently exist anywhere in this framework.

---

## 10. Automated analysis — Tier 2 rules (added Sep 26, 2026; Tier 2 built Sep 26–27)

Tier 2 is `sentinel_tier2.py`: it sends each Tier 1 escalation to Claude (Anthropic API, with web search) and writes a review to `reviews/`. Since Sep 27, 2026 it runs automatically once a week, inside Saturday's weekly job right after Tier 1 (never in the daily job: every review costs money). It can still be run by hand. It **applies** this framework; it never extends or overrides it. Consolidates `DECISIONS.md` items 37 and 46c.

- **Output per name:** a verdict in the ledger's vocabulary (watch / held / disqualified / parked / dismissed — every pass carries a specific re-add condition) plus the four §1 sub-scores as proposals (e.g. `3-2-4-3 = 12/20`), as §1 requires for Watch names. A score informs the decision; you make it.
- **Runs §1A in full:** insider classification (plan type, distinct insiders), 12-month price history plus 2–3 peers, the fraud-allegation check. It states explicitly which checks it could not complete, rather than issuing a verdict on partial diligence (the GPOR lesson).
- **Sources:** every hard date or figure carries a source URL, or is rejected. Primary sources (filings, IR releases, transcripts) beat secondhand summaries (the HUBS and RBA lessons).
- **No binary-catalyst predictions** (trial outcomes, drug approvals, contract awards): it may note that a dated binary event exists, never forecast its result.
- **Read-only:** never edits `positions.yaml`, `research_log.yaml`, the ledger or this file, and never places orders.
- **Out-of-scope vehicles** (§6A) are dismissed with that reason, not scored.
- **Cost caps:** token and search limits per run; cost is logged per report.
- **Calibration before live use:** it must match your own verdicts on a known set of names (the calibration set is recorded in `DECISIONS.md`, **deliberately not in this file**, since Tier 2 reads this file and would otherwise see the answers). During a calibration it is also not shown your `positions.yaml` notes. Passed Sep 27, 2026, 4/4, but with the answers visible in this file at the time, so the first 2–3 weeks of live reviews are checked by hand as well.

---

## Appendix A — Removed names log (summary)

| Name | Removed | Reason | Re-add condition |
|---|---|---|---|
| GOOGL, MSFT (META re-opened Aug 22 and bought Aug 26) | May 30, 2026 | Nasdaq-100 overlap — top-15 QQQ weight, owned via index | §3 presumption overridden by a real, named, dateable catalyst (changed Aug 22 from "≥20% dislocation only") |
| PANW, ADSK | May 30, 2026 | Originally flagged for Nasdaq-100 overlap, **plus independent issues**: PANW near 52-wk high + CyberArk dilution; ADSK's May 28 MaintainX acquisition dents the capital-light/buyback thesis | **Jun 14, 2026 — overlap veto lifted** (neither is a top-15 QQQ name under the refined §3 rule). Each now competes on the normal scorecard on its own merits; the independent issues above still apply and have not been resolved. |
| HON | May 30, 2026 | Originally flagged for Nasdaq-100 overlap, **plus** spin re-rating priced in (~20% rally, ~24.7x fwd) | **Jun 14, 2026 — overlap veto lifted** (not a top-15 QQQ name). Re-add still requires a post-spin standalone dislocation — the pricing concern stands independently of the overlap question. |
| IFC.TO | May 31, 2026 | Upside <20% at $272; 3 consecutive revenue misses | Pullback to ~$250 |
| HD | May 31, 2026 | Mgmt-confirmed 2027–28 housing recovery; upside <20% | Pullback to ~$280 + housing inflects |

---

*This framework supersedes the prior `Investment_Ledger_Stocks_Analysis.md`. Three-sleeve structure added June 2, 2026. Nasdaq-100 exclusion refined to top-15-by-weight on June 14, 2026. Discovery mechanism (Finviz two-step screen, §6A) added June 14, 2026. Pre-entry diligence checks (§1A — 10b5-1 classification, 12-month price history/peer comparison) added July 26, 2026 after a CHWY review surfaced a chronic multi-quarter decline and peer underperformance that a single-quarter read had missed. §6A Finviz filter set locked to exact values (Market Cap Mid $2–10bln, Operating Margin >0%, Analyst Recom. Buy or better, 52-Week Low 30%+ below, sorted by Performance Year ascending) and a standing per-session prompt added July 26, 2026, since Claude cannot run the screen itself. Price/Free Cash Flow (Under 30) and Debt/Equity (Under 1) added to the filter set July 26, 2026 — tunes this screen specifically as the Sleeve 1 pipeline; Sleeve 1B/2 candidates still need direct research since they won't reliably clear a valuation filter this tight. §1A check #3 (short-seller fraud/accounting allegations = automatic disqualifier) added July 28, 2026 after the SOFI/Muddy Waters case showed strong headline growth and insider buying aren't sufficient when the reported numbers themselves are under credible, unrebutted dispute. §2 non-US foreign domicile caveat and execution-discipline note added August 4, 2026 after ASML and LBRT were both executed in Non-Reg despite framework recommendations of TFSA/RRSP — surfaced that the RRSP withholding shield is a US-treaty-specific mechanism that doesn't automatically extend to non-US-domiciled names trading on US exchanges (e.g. Dutch-domiciled ASML), and that account-recommendation logic needs a final check at the point of order entry, not just at the research stage. §6B post-earnings reaction protocol added August 7, 2026 after four names (HLNE, SPOT, EFN, ATRL) reported in the same week with headline growth that didn't reliably predict the price reaction — codified checking the specific pre-defined gate condition over the headline number, waiting past day-of price action before calling a verdict, and re-verifying GTC targets against fresh consensus rather than assuming they're stale. **Five changes added September 25, 2026, at Claude's suggestion, in a review that found the scorecard's numeric totals were never actually being recorded despite §1 requiring them:** (1) §1 now requires scores to be recorded as numbers going forward, plus a mandatory head-to-head comparison when two Watch names compete for the same capital; (2) §1B sets a +20% floor under every Sleeve 1/1B exit target, raised only by a stronger sourced consensus figure, never lowered by a weaker one; (3) §3 adds a 15% combined cap on any named shared structural risk and resolves the 8%-cap-vs-let-winners-run contradiction (review at 15% portfolio weight, not an automatic trim, and the 8% cap still binds new money in); (4) §6C auto-parks a Watch name to Dormant after 6+ weeks with no new information and no gate within 8 weeks, replacing "no cap, no mechanism to shrink" with an explicit rule; (5) §9 adds a QQQ benchmark, per-position and portfolio-level, that did not exist anywhere before. The 15% figures in (3) are Claude's proposed defaults, not derived from anything already in the ledger — confirm or adjust them. Item 7 from the same review (execution discipline — confirming the account before submitting an order) was considered and deliberately left unchanged: it's by design, not a gap. **Sync pass, September 26, 2026 (logged in `DECISIONS.md`):** brought this file in line with changes the ledger had already made and with Sentinel now running — the §3/§5/Appendix A index exclusion rewritten as the Aug 22 presumption; the §6A filter table replaced with the Aug 22 verified set; screening and daily monitoring (§6, §6A, §7) now described as automated; BDCs and credit vehicles declared out of scope; §1A counts distinct insiders; §1B floor scope defined and the exits currently below it listed; new §10 sets Tier 2's rules; the ledger filename corrected throughout. **Sep 27, 2026:** Tier 2 built and calibrated; §1A insider rule corrected (AMRZ) and trivial purchases excluded; §1A check 3 clarified (class actions alone don't disqualify); §10 no longer contains the calibration answers. Confirmed by you on Sep 26: §1B floor scope (existing orders re-checked at next earnings) and §3 scope (top-15 QQQ plus all S&P 500, since you hold both funds). Tier 2 deferred; its output format stays undecided until it is built. Educational only — not investment or tax advice.*
