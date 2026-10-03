"""python -m p2.simulator [n_users]: create the warehouse datasets and land the shared user universe.

Experiment activity is landed later, per experiment, from the app or with simulate_experiment."""
import os
import sys

from p2.simulator.service import seed_universe
from p2.warehouse.bq import Warehouse

n = int(sys.argv[1]) if len(sys.argv) > 1 else 400_000
wh = Warehouse(env=os.environ.get("P2_ENV", "dev"))
wh.ensure()
print(f"environment {wh.env}: seeding {n:,} users")
seed_universe(wh, n)
print("done")
