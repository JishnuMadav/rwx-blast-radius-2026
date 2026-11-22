# RWX Blast Radius

For each independent SQLGlot patch, the selector reconstructs the changed
Python files against the pinned revision. It uses baseline measurements of
executed lines and functions, fixture and source reads, comparison operands,
callback invocations, individual dictionary values and scalar class settings.
Independent cold imports cover each test module. Shared setup dependencies
cover all member tests, including fixtures opened before individual methods run.
Statement alignment tracks insertions, deletions and changes to adjacent methods
independently. The selector unions readers across every hunk, includes cold
module initializers, and schedules the selected tests on one to eight workers,
including the two-second cost per worker. Unsupported patch formats and
changes that fail to compile select the full catalog.

The organizer charges the scheduled worker time plus the selector's CPU time.
Every patch that omits a failing test adds a 300-second penalty.

## Run the official sample

Unpack the organizers' Blast Radius starter kit into `.local/sample/`.
From this project directory in a Linux shell with Docker and Make, run:

~~~sh
docker build --platform linux/amd64 -t rwx-blast-selector .
make -C .local/sample -o build sample IMAGE=rwx-blast-selector OUTPUT=../candidate.jsonl
python -m unittest test_selector test_witness
~~~

The image runs offline using the included baseline maps and the
checksum-verified source checkout downloaded during the build. The sample
answers are used by the external scorer. The reported simulated time excludes
the selector's CPU time, which the competition adds on its servers.

## Rebuild the coverage map

Use Linux with Python 3.13 and the organizer's pinned requirements. Put
the environment's bin directory first on PATH, including for child processes. Check out
SQLGlot at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` under `.local/sqlglot/`.
Then run:

~~~sh
python build_map.py .local/sqlglot .local/sample/sample/tests.json coverage.json.gz
python build_map.py .local/sqlglot .local/sample/sample/tests.json coverage.json.gz --augment-workers
python verify_coverage.py .local/sqlglot coverage.json.gz coverage.json.gz
python profile_semantics.py .local/sqlglot .local/sample/sample/tests.json .local/operand-map.gz
python profile_scalars.py .local/sqlglot .local/sample/sample/tests.json .local/scalar-map.gz
python profile_dispatch.py .local/sqlglot .local/sample/sample/tests.json .local/dispatch-map.gz
python profile_class_reads.py .local/sqlglot .local/sample/sample/tests.json .local/class-map.gz
python profile_guards.py .local/sqlglot .local/sample/sample/tests.json .local/guard-map.gz
for family in methods effects branches copies identity switches validators cold_reads startup predicates forwarding properties; do
  python "profile_${family}.py" .local/sqlglot .local/sample/sample/tests.json ".local/${family}-map.gz"
done
python profile_cold_reads.py .local/sqlglot .local/sample/sample/tests.json .local/cold-predicate-map.gz --predicates
python merge_profiles.py semantics.json.gz .local/operand-map.gz .local/scalar-map.gz .local/dispatch-map.gz .local/class-map.gz .local/guard-map.gz .local/methods-map.gz .local/effects-map.gz .local/branches-map.gz .local/copies-map.gz .local/identity-map.gz .local/switches-map.gz .local/validators-map.gz .local/cold_reads-map.gz .local/startup-map.gz .local/predicates-map.gz .local/cold-predicate-map.gz .local/forwarding-map.gz .local/properties-map.gz
python profile_lifecycle.py .local/sqlglot .local/sample/sample/tests.json lifecycle.json.gz
python profile_imports.py .local/sqlglot .local/sample/sample/tests.json imports.json.gz
~~~

For a faster semantic profile, start multiple copies with the same
`--shards N` and distinct `--shard` indices from zero through N minus one.
Give each copy its own gzip output. Merge the shards of each profile first
and verify all catalog tests passed before combining the profile families.

The map is derived from the unmodified SQLGlot revision. The measurement
records test execution in the pytest process, including setup and teardown.
A second trace replays process-pool calls synchronously with argument and
result serialization, then merges their hits with the original trace.
Dynamic imports, process-specific behavior and shared fixture setup can introduce
dependencies that a single baseline execution misses. Runtime and platform
differences can also affect the map. Tests without a verified baseline pass
are retained conservatively.

The catalog, sample patches, external scorer, container flags, and pinned
runtime image come from the organizers' kit. This project's selector and
coverage recorder are original MIT-licensed code.
