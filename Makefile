SRC := ../../cursor/python_mcp/server
SRV := ../../cursor/ragtag/python/ragtag/src/ragtag

update:
	@export SRC=$(SRC); \
	for FN in $$( find -L . -type f -not -path './.git/*'  -not -path './server/*' -not -path './.gitignore' -not -path './.cursorindexingignore' ); do \
		if [ -f "$$SRC/$$FN" ]; then \
			echo "Copying: $$SRC/$$FN → $$FN"; \
			cp -a -L -f "$$SRC/$$FN" "$$FN"; \
		else \
			echo "Skipping (not in SRC): $$FN"; \
		fi; \
	done
	@export SRC=$(SRV); \
	for FN in $$( find -L server -type f -not -path 'server/.git/*' -not -path 'server/.gitignore' -not -path 'server/.cursorindexingignore' -printf '%P\n' ); do \
		if [ -f "$$SRC/$$FN" ]; then \
			echo "Copying: $$SRC/$$FN → server/$$FN"; \
			cp -a -L -f "$$SRC/$$FN" "server/$$FN"; \
		else \
			echo "Skipping (not in SRC): server/$$FN"; \
		fi; \
	done

