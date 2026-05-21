#!/usr/bin/env python3
"""
Convenience entry point so you can run the tool without installing it:

    python patient_maker.py --portfolio my_portfolio.json --capital 10000000

It just puts the bundled ``src/`` directory on the import path and hands off to
``kis_passive_trader.patient_maker:main``. If you installed the package
(``pip install -e .``), use the ``patient-maker`` console command instead.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from kis_passive_trader.patient_maker import main   # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
