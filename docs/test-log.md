# Test log

Record each test result here: the date, the command, the setup and the measured value. Do not record a result that you did not measure.

CI runs on shared virtual machines with 2 cores. A process there can stall for 100 to 300 ms. Thus CI latency gates only find a gross regression (stop p95 250 ms or less, owner close 250 ms or less). The plan targets (stop p99 75 ms or less, owner close 100 ms or less) apply to local and hardware runs. Record those runs here.

| Date | Phase | Command | Setup | Result |
| --- | --- | --- | --- | --- |
| 2026-10-25 | 1 | `python -m unittest discover -s tests` (3 runs) | MacBook, Python 3.11.15, fake bridge | 51 tests pass in each run. Stop latency p99: 12.97, 0.91, 1.51 ms (target 75 ms). After `kill -9 robotd` in a hold: last repeat 130, 235, 142 ms (limit 400 ms); FAULT 821, 824, 823 ms (limit 1100 ms). Simulated goto: 10 of 10 goals. |
| 2026-10-25 | 1 | `python -m unittest discover -s tests` (2 runs, after review fixes) | MacBook, Python 3.11.15, fake bridge | 62 tests pass in each run. Stop latency p99: 2.81, 0.61 ms. Owner close to stop: 0.2, 0.4 ms. After `kill -9 robotd` in a hold: last repeat 157, 163 ms; FAULT 822, 825 ms. |
