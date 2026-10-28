# Configures a UVKBuildTool project (its uvproj.yaml) for a system-wide build
# against the system's framework. Upstream installs libraries to lib64/, which
# abuild rejects, so everything goes to lib/ instead. The directories are
# compiled into the application. abuild runs this with busybox sh: no bashisms.
_configure_project() {
	local file="$1" framework="${2:-false}"
	sed -i \
		-e 's|lib64/|lib/|g' \
		-e '/^build-mode-vendor:/d' \
		-e '/^system-wide:/d' \
		-e '/^install-framework:/d' \
		"$file"
	if grep -q '^install-override:' "$file"; then
		sed -i '/^install-override:/,/^[^ ]/ s|^  unix:$|  unix:\n    framework-library-dir: "lib/"\n    framework-application-library-dir: "lib/"|' "$file"
	else
		printf 'install-override:\n  unix:\n    framework-library-dir: "lib/"\n    framework-application-library-dir: "lib/"\n' >> "$file"
	fi
	printf 'build-mode-vendor: false\nsystem-wide: true\ninstall-framework: %s\n' "$framework" >> "$file"
}

# The job count abuild's MAKEFLAGS asks for (JOBS in abuild.conf), for
# UIMGUI_JOBS (the framework's scripts otherwise use every CPU). Empty without one.
_make_jobs() {
	printf '%s\n' "$MAKEFLAGS" | sed -n 's/.*-j *\([0-9][0-9]*\).*/\1/p'
}

# UVKBuildTool exits with 0 even when the build fails: fail when the project's
# library (lib<name>Lib.so, <name> from uvproj.yaml) wasn't installed
_check_installed() {
	local name
	name=$(sed -n 's/^name: *"\{0,1\}\([^"]*\)"\{0,1\} *$/\1/p' "$1" | head -n 1)
	if [ ! -e "$2/lib${name}Lib.so" ]; then
		echo "UVKBuildTool didn't install lib${name}Lib.so, so the build failed (see above)" >&2
		return 1
	fi
}
