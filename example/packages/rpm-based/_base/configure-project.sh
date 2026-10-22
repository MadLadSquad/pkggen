# Configures a UVKBuildTool project (its uvproj.yaml) for a system-wide build
# against the system's framework, with every library in %%{_libdir}
# (upstream assumes lib64/). The directories are compiled into the application.
configure_project() {
	file=$1 framework=${2:-false}
	sed -i \
		-e 's|lib64/|%{_lib}/|g' \
		-e '/^build-mode-vendor:/d' \
		-e '/^system-wide:/d' \
		-e '/^install-framework:/d' \
		"$file"
	if grep -q '^install-override:' "$file"; then
		sed -i '/^install-override:/,/^[^ ]/ s|^  unix:$|  unix:\n    framework-library-dir: "%{_lib}/"\n    framework-application-library-dir: "%{_lib}/"|' "$file"
	else
		printf 'install-override:\n  unix:\n    framework-library-dir: "%{_lib}/"\n    framework-application-library-dir: "%{_lib}/"\n' >> "$file"
	fi
	printf 'build-mode-vendor: false\nsystem-wide: true\ninstall-framework: %%s\n' "$framework" >> "$file"
}
