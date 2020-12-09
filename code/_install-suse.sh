#!/bin/bash

# Python version to use needs to be provided by our caller
PY_BINARY=$1
echo "*** Zato SUSE installation using $PY_BINARY ***"

# if [[ "$(type -p SUSEConnect)" ]]
# then
#     SUSEConnect -p PackageHub/15.2/x86_64
# fi

sudo zypper install -y \
    curl cyrus-sasl-devel gcc gcc-c++ haproxy libbz2-devel libev-devel libev4 \
    libffi-devel keyutils-devel libmemcached-devel libpqxx-devel              \
    openldap2-devel libxml2-devel libxslt-devel libyaml-devel openssl         \
    patch $PY_BINARY $PY_BINARY-devel $PY_BINARY-pip uuid-devel               \
    wget zlib-devel postgresql-server-devel

curl https://bootstrap.pypa.io/get-pip.py | $(type -p $PY_BINARY)
$PY_BINARY -m pip install       \
    --no-warn-script-location   \
    -U virtualenv

$PY_BINARY -m virtualenv .
source ./bin/activate
./bin/$PY_BINARY -m pip install -U setuptools pip
source ./_postinstall.sh $PY_BINARY
