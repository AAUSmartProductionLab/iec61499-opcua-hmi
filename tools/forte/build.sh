#!/usr/bin/env bash
# 4diac FORTE 3.3.0 for Linux (x86_64) with ModLib and every module of iec61499-mgmt-py, without
# the FBE: what runtime/build-modules.ps1 and build-runtime.ps1 do on Windows, for the end to end
# test (tests/test_end_to_end.py) and for running the modules on a Linux PC.
#
#   tools/forte/build.sh [WORK_DIR]          # default ../forte-build; prints the binary's path
#
# Needs git, a C/C++ compiler, CMake >= 3.30 (pip install cmake), Java (for the 4diac IDE),
# xvfb-run and libmodbus-dev, and a checkout of iec61499-mgmt-py next to this repository (or
# IEC61499_MGMT_PY). Every step is skipped when its result is there already.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
MGMT="${IEC61499_MGMT_PY:-$REPO/../iec61499-mgmt-py}"
WORK="$(mkdir -p "${1:-$REPO/../forte-build}" && cd "${1:-$REPO/../forte-build}" && pwd)"
FORTE_TAG=3.3.0
OPEN62541_TAG=v1.5.4          # the version FORTE 3.3.0 asks for
IDE_URL=https://download.eclipse.org/4diac/releases/3.3/4diac-ide/4diac-ide_3.3.0-linux.gtk.x86_64.tar.gz
JOBS="$(nproc)"
mkdir -p "$WORK/logs"
cd "$WORK"

if [ ! -x 4diac-ide/4diac-ide ]; then
    echo "4diac IDE 3.3.0"
    curl -sSL -o ide.tar.gz "$IDE_URL" && tar xzf ide.tar.gz && rm ide.tar.gz
fi

if [ ! -d forte ]; then
    echo "4diac FORTE $FORTE_TAG with the patches of iec61499-mgmt-py"
    git -c advice.detachedHead=false clone -q --depth 1 --branch "$FORTE_TAG" https://github.com/eclipse-4diac/4diac-forte.git forte
    for patch in "$MGMT"/runtime/forte-patches/*.patch; do git -C forte apply "$patch"; done
fi

if [ ! -f prefix/lib/libopen62541.a ]; then
    echo "open62541 $OPEN62541_TAG (single threaded, as the FBE configurations say)"
    [ -d open62541 ] || git -c advice.detachedHead=false clone -q --depth 1 --branch "$OPEN62541_TAG" https://github.com/open62541/open62541.git open62541
    git -C open62541 submodule update --init --depth 1 deps/ua-nodeset
    cmake -S open62541 -B open62541/build -DCMAKE_BUILD_TYPE=RelWithDebInfo -DCMAKE_INSTALL_PREFIX="$WORK/prefix" \
        -DBUILD_SHARED_LIBS=OFF -DUA_MULTITHREADING=0 -DUA_NAMESPACE_ZERO=FULL -DUA_ENABLE_ENCRYPTION=OFF \
        -DUA_BUILD_EXAMPLES=OFF -DUA_ENABLE_PUBSUB=OFF -DCMAKE_POSITION_INDEPENDENT_CODE=ON > logs/open62541.log
    cmake --build open62541/build -j "$JOBS" --target install >> logs/open62541.log
fi

echo "types of ModLib and the modules, exported by the 4diac IDE"
python "$HERE/export_types.py" "$WORK"

echo "FORTE"
# The module set of the FBE configurations (configurations/inc/default.txt, modules-win.txt):
# OPC UA, Modbus, local and network communication, the standard modules, IO with GPIO and PWM.
cmake -S "$HERE" -B build-forte -DCMAKE_BUILD_TYPE=RelWithDebInfo -DFORTE_SOURCE_DIR="$WORK/forte" \
    -DMODULES_DIR="$WORK/modules" -DCMAKE_PREFIX_PATH="$WORK/prefix" -DFORTE_ARCHITECTURE=Posix \
    -DFORTE_IO=ON -DFORTE_COM_ETH=ON -DFORTE_COM_FBDK=ON -DFORTE_COM_LOCAL=ON -DFORTE_COM_RAW=ON \
    -DFORTE_COM_SER=ON -DFORTE_COM_OPC_UA=ON -DFORTE_COM_MODBUS=ON -DFORTE_COM_STRUCT_MEMBER=ON \
    -DFORTE_MODULE_CONVERT=ON -DFORTE_MODULE_IEC61131=ON -DFORTE_MODULE_RECONFIGURATION=ON \
    -DFORTE_MODULE_RT_Events=ON -DFORTE_MODULE_SIGNALPROCESSING=ON -DFORTE_MODULE_UTILS=ON \
    -DFORTE_MODULE_GPIOCHIP=ON -DFORTE_MODULE_PWMSYSFS=ON -DFORTE_COM_OPC_UA_MULTICAST=OFF \
    -DFORTE_COM_OPC_UA_AC=OFF -DFORTE_STACKTRACE=OFF \
    -DFORTE_EventChainExternalEventListSize=256 -DFORTE_CommunicationInterruptQueueSize=64 \
    -DCMAKE_CXX_FLAGS="-I/usr/include/modbus" > logs/cmake-forte.log
cmake --build build-forte -j "$JOBS" --target forte > logs/make-forte.log
echo "$WORK/build-forte/forte/forte"
