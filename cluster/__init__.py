"""cluster -- LAN heterogeneous job-pool substrate (docs/CLUSTER_PLAN.md).

Docker workers pull independent jobs from one scheduler, matched to hardware by capability tag. Control
plane = gRPC; data plane = the sha256-verified scp bundle transport. Filesystem+JSON queue, single
scheduler, no K8s/Slurm/Ray/broker. Authored on a no-Python laptop -- every module is VERIFY ON CLUSTER
and ships a local self-test (no network/Docker/GPU): `python -m cluster.<mod> selftest`.
"""
