#!/bin/sh
# Builds dist/taxmod-printservice_<version>_all.deb from the working tree. The version is read from the agent.
set -eu
cd "$(dirname "$0")/.."
version=$(sed -n 's/^VERSION = "taxmod-print-agent \(.*\)"$/\1/p' src/taxmod_print_agent.py)
[ -n "$version" ] || { echo "no VERSION in src/taxmod_print_agent.py" >&2; exit 1; }
if [ -n "${1:-}" ] && [ "$1" != "v$version" ]; then
    echo "tag $1 does not match version $version" >&2; exit 1
fi
root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT
install -d "$root/DEBIAN" "$root/usr/bin" "$root/usr/lib/taxmod-printservice" "$root/usr/lib/systemd/user" "$root/usr/share/taxmod-printservice" "$root/usr/share/doc/taxmod-printservice"
sed "s/@VERSION@/$version/" packaging/debian/control > "$root/DEBIAN/control"
install -m 755 packaging/debian/postinst packaging/debian/prerm "$root/DEBIAN/"
install -m 644 src/taxmod_print_agent.py "$root/usr/lib/taxmod-printservice/"
install -m 755 src/taxmod_printservice.py "$root/usr/lib/taxmod-printservice/"
ln -s ../lib/taxmod-printservice/taxmod_printservice.py "$root/usr/bin/taxmod-printservice"
install -m 644 packaging/taxmod-printservice.service "$root/usr/lib/systemd/user/"
install -m 644 config.example.json "$root/usr/share/taxmod-printservice/"
install -m 644 README.md LICENSE "$root/usr/share/doc/taxmod-printservice/" 2>/dev/null || install -m 644 README.md "$root/usr/share/doc/taxmod-printservice/"
mkdir -p dist
dpkg-deb --root-owner-group --build "$root" "dist/taxmod-printservice_${version}_all.deb"
