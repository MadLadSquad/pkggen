#!/bin/sh
# Helpers for UVKBuildTool projects, called from debian/rules:
#   sh debian/uimgui.sh configure <uvproj.yaml> <libdir> [install-framework]
#   sh debian/uimgui.sh check <uvproj.yaml> <installed library directory>
set -e

case "$1" in
configure)
	# A system-wide build against the system's framework, with every library
	# in the multiarch directory (upstream assumes lib64/). The directories are
	# compiled into the application.
	file=$2 libdir=$3 framework=${4:-false}
	sed -i \
		-e "s|lib64/|$libdir/|g" \
		-e '/^build-mode-vendor:/d' \
		-e '/^system-wide:/d' \
		-e '/^install-framework:/d' \
		"$file"
	if grep -q '^install-override:' "$file"; then
		sed -i "/^install-override:/,/^[^ ]/ s|^  unix:\$|  unix:\n    framework-library-dir: \"$libdir/\"\n    framework-application-library-dir: \"$libdir/\"|" "$file"
	else
		printf 'install-override:\n  unix:\n    framework-library-dir: "%s/"\n    framework-application-library-dir: "%s/"\n' "$libdir" "$libdir" >> "$file"
	fi
	printf 'build-mode-vendor: false\nsystem-wide: true\ninstall-framework: %s\n' "$framework" >> "$file"
	;;
check)
	# UVKBuildTool exits with 0 even when the build fails: fail when the
	# project's library (lib<name>Lib.so, <name> from uvproj.yaml) wasn't installed
	name=$(sed -n 's/^name: *"\{0,1\}\([^"]*\)"\{0,1\} *$/\1/p' "$2" | head -n 1)
	if [ ! -e "$3/lib${name}Lib.so" ]; then
		echo "UVKBuildTool didn't install lib${name}Lib.so, so the build failed (see above)" >&2
		exit 1
	fi
	;;
*)
	echo "usage: $0 configure|check ..." >&2
	exit 2
	;;
esac
