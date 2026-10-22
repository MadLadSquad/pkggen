# UVKBuildTool exits with 0 even when the build fails: fail when the project's
# library (lib<name>Lib.so, <name> from uvproj.yaml) wasn't installed
check_installed() {
	name=$(sed -n 's/^name: *"\{0,1\}\([^"]*\)"\{0,1\} *$/\1/p' "$1" | head -n 1)
	if [ ! -e "$2/lib${name}Lib.so" ]; then
		echo "UVKBuildTool didn't install lib${name}Lib.so, so the build failed (see above)" >&2
		exit 1
	fi
}
