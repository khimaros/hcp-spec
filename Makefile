# hcp-spec is primarily the protocol spec; the shared conformance suite and the
# canonical hello hook fixture live under conformance/ (see conformance/README.md).
# `make test` runs the fixture's standalone hook test and byte-compiles the
# shared driver. the reference hosts run their own conformance via the driver.

test:
	python3 conformance/fixtures/hello/tests/hello_test.py
	python3 -m py_compile conformance/hcpconform.py
.PHONY: test
