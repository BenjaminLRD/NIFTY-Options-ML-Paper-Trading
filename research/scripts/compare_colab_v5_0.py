"""Compare a Colab-generated V5_0_Reports.zip with the local one, report by report.

Usage: python compare_colab_v5_0.py COLAB_V5_0_Reports.zip [LOCAL_V5_0_Reports.zip]
Exit code 0 only when every report is byte-identical or numerically equal (tolerance 1e-9).
"""
import io
import sys
import zipfile

import pandas as pd

colab = zipfile.ZipFile(sys.argv[1])
local = zipfile.ZipFile(sys.argv[2] if len(sys.argv) > 2 else "V5_0_Reports.zip")
cn, ln = set(colab.namelist()), set(local.namelist())
bad = 0
for n in sorted(cn | ln):
    if n not in cn or n not in ln:
        print("ONLY IN", "colab" if n in cn else "local", n)
        bad += 1
        continue
    a, b = colab.read(n), local.read(n)
    if a == b:
        print("identical ", n)
        continue
    try:
        da, db = pd.read_csv(io.BytesIO(a)), pd.read_csv(io.BytesIO(b))
        pd.testing.assert_frame_equal(da, db, check_exact=False, atol=1e-9, rtol=1e-9)
        print("equal(tol)", n)
    except Exception as e:
        print("DIFFERENT ", n, "-", str(e).splitlines()[0][:150])
        bad += 1
print("RESULT:", "MATCH" if not bad else f"{bad} discrepancies")
sys.exit(1 if bad else 0)
