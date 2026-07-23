.PHONY: clean

clean:
	@echo "Cleaning up __pycache__ and bytecode files..."
	@find . -type d -name "__pycache__" -exec rm -rf {} +
	@find . -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete
	@echo "Done."