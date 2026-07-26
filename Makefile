.PHONY: image test check

image:
	docker build --platform linux/arm64 -t stt-to-subtitle:kotoba-m1 .

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v

check:
	PYTHONPATH=src python3 -m compileall -q src scripts tests
	git diff --check
