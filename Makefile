SRC := ../../cursor/python_mcp/server

update:
	@export SRC=$(SRC); \
	for FN in $$(find . -type f | grep -v '^\.\/\.git'); do \
		if [ -f "$$SRC/$$FN" ]; then \
			echo "Copying: $$SRC/$$FN → $$FN"; \
			cp -a -L -f "$$SRC/$$FN" "$$FN"; \
		else \
			echo "Skipping (not in SRC): $$FN"; \
		fi; \
	done

