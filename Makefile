.PHONY: test compile clean

test:
	python3 -m unittest discover -v

compile:
	python3 -m py_compile lfb/*.py

clean:
	rm -rf build dist *.egg-info .pytest_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
