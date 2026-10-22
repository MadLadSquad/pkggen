# Copyright 2022 - 2026 MadLadSquad
# Distributed under the terms of the MIT License

# @ECLASS: uimgui.eclass
# @MAINTAINER:
# MadLadSquad <contact@madladsquad.com>
# @AUTHOR:
# MadLadSquad <contact@madladsquad.com>
# Stanislav Vasilev (Madman10K) <contact@i-use-gentoo-btw.com>
# @SUPPORTED_EAPIS: 8
# @BLURB: Builds and installs applications made with the UntitledImGuiFramework
# @DESCRIPTION:
# Applications made with the UntitledImGuiFramework are built and installed in
# one step by the framework's build tool, UVKBuildTool. This eclass generates the
# project's build files, points the project's library directories at the
# system's (get_libdir) and builds it into the image in src_install.
#
# Ebuilds with extra parts (Go or Rust libraries the project installs) build
# them in src_compile and then call uimgui_src_compile.

case ${EAPI} in
	8) ;;
	*) die "${ECLASS}: EAPI ${EAPI:-0} not supported" ;;
esac

if [[ -z ${_UIMGUI_ECLASS} ]]; then
_UIMGUI_ECLASS=1

inherit multilib multiprocessing

# UVKBuildTool comes with the framework, and runs CMake and make
DEPEND="ude-base/untitled-imgui-framework"
RDEPEND="${DEPEND}"
BDEPEND="
	ude-base/untitled-imgui-framework
	dev-build/cmake
	virtual/pkgconfig
"

# @FUNCTION: uimgui_configure_project
# @USAGE: <uvproj.yaml> [install-framework]
# @DESCRIPTION:
# Configures a project for a system-wide, non-vendored build: the framework
# comes from the system, and everything that would go to lib64/ goes to the
# system's library directory. The directories are compiled into the
# application, so they must be the final ones.
uimgui_configure_project() {
	local file=${1} framework=${2:-false} libdir
	libdir=$(get_libdir)

	sed -i \
		-e "s|lib64/|${libdir}/|g" \
		-e "/^build-mode-vendor:/d" \
		-e "/^system-wide:/d" \
		-e "/^install-framework:/d" \
		"${file}" || die

	if grep -q "^install-override:" "${file}"; then
		# Add the library directories to the project's own overrides
		sed -i "/^install-override:/,/^[^ ]/ s|^  unix:\$|  unix:\n    framework-library-dir: \"${libdir}/\"\n    framework-application-library-dir: \"${libdir}/\"|" \
			"${file}" || die
	else
		cat >> "${file}" <<-EOF || die
		install-override:
		  unix:
		    framework-library-dir: "${libdir}/"
		    framework-application-library-dir: "${libdir}/"
		EOF
	fi
	cat >> "${file}" <<-EOF || die
	build-mode-vendor: false
	system-wide: true
	install-framework: ${framework}
	EOF
}

# @FUNCTION: uimgui_check_installed
# @USAGE: <uvproj.yaml> <library directory>
# @DESCRIPTION:
# UVKBuildTool exits with 0 even when the build fails: dies when the
# project's library (lib<name>Lib.so, <name> from uvproj.yaml) wasn't installed.
uimgui_check_installed() {
	local name
	name=$(sed -n 's/^name: *"\{0,1\}\([^"]*\)"\{0,1\} *$/\1/p' "${1}" | head -n 1)
	[[ -e ${2}/lib${name}Lib.so ]] || die "UVKBuildTool didn't install lib${name}Lib.so, so the build failed"
}

# @FUNCTION: uimgui_src_configure
# @DESCRIPTION:
# Generates the project's build files and configures it.
uimgui_src_configure() {
	UVKBuildTool --generate "${S}" || die
	uimgui_configure_project "${S}/uvproj.yaml"
}

# @FUNCTION: uimgui_src_compile
# @DESCRIPTION:
# Nothing: UVKBuildTool builds and installs in one step, in src_install.
uimgui_src_compile() {
	:
}

# @FUNCTION: uimgui_src_install
# @DESCRIPTION:
# Builds the project and installs it into the image, with as many jobs as
# MAKEOPTS asks for (the framework's build script reads UIMGUI_JOBS).
uimgui_src_install() {
	export UIMGUI_JOBS=$(makeopts_jobs)
	UVKBuildTool --build "${ED}/usr" "${EPREFIX}/usr" "${S}" || die
	uimgui_check_installed "${S}/uvproj.yaml" "${ED}/usr/$(get_libdir)"
	# The framework's copy of utfcpp comes from dev-libs/utfcpp
	rm -rf "${ED}"/usr/{include,share}/utf8cpp || die
	einstalldocs
}

fi

EXPORT_FUNCTIONS src_configure src_compile src_install
