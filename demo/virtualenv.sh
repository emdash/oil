#!/bin/bash
#
# Run scripts generated by Python's virtualenv under osh.
#
# Usage:
#   ./virtualenv.sh <function name>

set -o nounset
set -o pipefail
set -o errexit

readonly DIR=_tmp/test-venv

create() {
  virtualenv $DIR
}

under-bash() {
	# You can see PS1 change here.
	bash -i <<EOF
source $DIR/bin/activate
echo DONE
EOF
}

# Hm there seem to be multiple things that don't work here.
under-osh() {
	bin/osh -i <<EOF
source $DIR/bin/activate
echo DONE
EOF
}

"$@"
