# Sentinel

A personal stock-research pipeline that runs on a Mac mini: it screens for candidates (Finviz), watches the
holdings and the watch list (prices, earnings dates), flags what deserves a look with mechanical rules (Tier 1),
and has Claude review each flag against the written investment framework (Tier 2). It never trades and never
edits the investor's files: it finds and watches; the investor decides.

- `Investment_Strategy_Framework.md`: the rules. `DECISIONS.md`: every design decision, append-only.
- `positions.yaml`, `research_log.yaml`: the investor's holdings, watch list and reviewed names (edited by hand).
- `sentinel_*.py`: the programs; `test_sentinel_*.py`: their tests (plain Python, last line `PASS n/n`).
- `reviews/`: Tier 2's written reviews, one file per day.

Educational analysis only; not investment advice.
