"""The V5.1 research result, copied as a record (not recomputed, not reproduced). Source: V5_1_Final_Research_Report.md, section 6.2, in the frozen research folder.

Real historical NSE settlement data was used by that study. This program does not hold or use that data, and the numbers below are the research's own: a Brier
difference below 0 would mean better than the prior. None of the four was supported (every interval includes 0). They are shown so the dashboard cannot be read as
saying the research models work.
"""
RESEARCH_SOURCE = "V5_1_Final_Research_Report.md section 6.2 (V5.1 one-shot evaluation, trade dates 2026-06-30 to 2026-09-28); copied, not recomputed"
RESEARCH_RESULTS = [
    {"option": "CE", "model": "B1 (lr1)", "prior_brier": "0.23987", "model_brier": "0.24205", "difference": "+0.00218", "interval": "-0.00042 to +0.00441", "supported": False},
    {"option": "CE", "model": "B2 (ur1)", "prior_brier": "0.23987", "model_brier": "0.24035", "difference": "+0.00048", "interval": "-0.00096 to +0.00150", "supported": False},
    {"option": "PE", "model": "B1 (lr1)", "prior_brier": "0.24891", "model_brier": "0.24895", "difference": "+0.00004", "interval": "-0.00169 to +0.00144", "supported": False},
    {"option": "PE", "model": "B2 (ur1)", "prior_brier": "0.24891", "model_brier": "0.24892", "difference": "+0.00001", "interval": "-0.00078 to +0.00059", "supported": False},
]
