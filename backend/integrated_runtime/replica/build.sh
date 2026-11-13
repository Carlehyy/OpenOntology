#!/bin/sh
# Build the OpenOntology replica SMT (TransportMetadata + ReadOffsets) against a
# local Debezium installation. Fully offline: only jars under <debezium-home>/lib
# are used; sources stay in this directory, all artifacts go to <output-dir>.
#
# Usage: build.sh <debezium-home> <output-dir>
# Example: sh build.sh /debezium /artifacts
set -eu

if [ "$#" -ne 2 ]; then
    echo "usage: $0 <debezium-home> <output-dir>" >&2
    exit 2
fi

debezium_home=$1
output_dir=$2
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ ! -d "$debezium_home/lib" ]; then
    echo "error: $debezium_home/lib not found (pass the Debezium home, e.g. /debezium)" >&2
    exit 2
fi

if ! command -v javac >/dev/null 2>&1; then
    echo "error: javac not on PATH" >&2
    exit 2
fi

java_bin=$(command -v java)
java_home=${java_bin%/bin/java}
jar_bin=$java_home/bin/jar
if [ ! -x "$jar_bin" ] && command -v jar >/dev/null 2>&1; then
    jar_bin=jar
fi
if [ ! -x "$jar_bin" ] && [ "$jar_bin" != "jar" ]; then
    echo "error: jar tool not found next to javac" >&2
    exit 2
fi

classpath=
for jar in "$debezium_home"/lib/*.jar; do
    classpath="$classpath:$jar"
done

mkdir -p "$output_dir/classes" "$output_dir/test-classes"

echo "javac: $(javac -version 2>&1)"

# Production classes: the SMT plus the ReadOffsets CLI (selftest stays out of the jar).
javac --release 21 -encoding UTF-8 -cp "$classpath" \
    -d "$output_dir/classes" \
    "$script_dir/TransportMetadata.java" \
    "$script_dir/ReadOffsets.java"

javac --release 21 -encoding UTF-8 -cp "$classpath:$output_dir/classes" \
    -d "$output_dir/test-classes" \
    "$script_dir/SelfTest.java"

"$jar_bin" --create --file "$output_dir/replica-smt.jar" -C "$output_dir/classes" .

echo "built: $output_dir/replica-smt.jar"
echo "selftest: java -cp \"$output_dir/classes:$output_dir/test-classes:$debezium_home/lib/*\" io.openontology.replica.SelfTest"
