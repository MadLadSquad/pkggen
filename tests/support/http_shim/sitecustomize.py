# Loaded automatically by every Python subprocess the tests start (this folder is
# on PYTHONPATH). When PKGGEN_TEST_HTTP names a routes file, all requests calls in
# the process are answered from it, so generators run offline.
import os

if os.environ.get("PKGGEN_TEST_HTTP"):
    import pkggen_test_http

    pkggen_test_http.start_from_file(os.environ["PKGGEN_TEST_HTTP"], os.environ.get("PKGGEN_TEST_HTTP_LOG"))
