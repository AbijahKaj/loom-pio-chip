#!/bin/sh
# Full local regression. Usage: ./check.sh [--quick]
#   --quick : skip the RTL suites (unit tests, ISS scenarios and formal only)
set -e
cd "$(dirname "$0")"
PY=${PY:-python3}
VENV_BIN=$(pwd)/.venv/bin
echo "== toolchain unit tests";        $PY test/unit_tests.py | tail -1
echo "== protocol scenarios on ISS";   $PY test/run_iss.py | grep -cE "^PASS" | sed 's/$/ scenarios passed/'
echo "== formal";                      formal/run.sh
[ "$1" = "--quick" ] && exit 0
echo "== RTL: smoke, protocols, host driver, lockstep diff"
cd test && PATH=$VENV_BIN:$PATH make -B NODUMP=1 COCOTB_TEST_MODULES=test,test_protocols,test_host,test_diff \
  2>&1 | grep -E "\*\* test|TESTS="
