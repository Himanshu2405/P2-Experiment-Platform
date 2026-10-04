"""Entry point for the public read-only demo (for example on Streamlit Community Cloud).

It runs the same app on a frozen copy of the app tables (demo/app_tables.json.gz), so it needs no BigQuery and no credentials.
Every page can be browsed; adding, editing and running experiments is turned off.

    streamlit run app/demo_app.py
"""
import os
import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))     # the p2 package, without installing it
os.environ["P2_STORE"] = "demo"
runpy.run_path(str(HERE / "streamlit_app.py"), run_name="__main__")
