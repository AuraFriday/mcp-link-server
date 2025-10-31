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
	cp -a ~/website_aura/downloads/checksums.txt .


release: update print-latest-steps
	@VERSION=$$(perl -ne 'if(/AuraFriday-mcp-link-server-setup-v([\d\.]+)/){print "$$1";exit}' README.md); \
	echo "[release] TODO: add GitHub upload script here for v $$VERSION"
	git status


print-latest-steps:
	@echo
	@echo "Next steps for LATEST-ONLY release:"
	@echo "1) Commit staged changes in your GitHub repo tree (no binaries):"
	@echo "   cd ~/Downloads/repos/mcp-link-server/"
	@echo "   git status"
	@echo "   git add ."
	@echo "   git commit -m \"Release: update source for latest\""
	@echo
	@echo "2) Update the 'latest' tag to this commit and push:"
	@echo "   git tag -fa latest -m \"Version v$(VERSION) $$(date +%F)\""
	@echo "   git push"
	@echo "   git push --force --tags"
	@echo
	@echo "3) Upload the binaries:"
	@echo "   GH_TOKEN=\"ghp_(token)\" gh release edit latest --title \"Latest\" --notes \"Latest release v$(VERSION) built on $$(date +%F)\" "
	@echo "   GH_TOKEN=\"ghp_(token)\" gh release upload latest release/AuraFriday-mcp-link-server-setup-v1.2.47-mac-arm.pkg release/AuraFriday-mcp-link-server-setup-v1.2.47-mac-intel.pkg release/AuraFriday-mcp-link-server-setup-v1.2.47-linux-x86_64.run release/AuraFriday-mcp-link-server-setup-v1.2.47-windows-x86_64.exe --clobber"
	@echo
	@echo "Stable user download links:"
	@echo "   https://github.com/AuraFriday/mcp-link-server/releases/tag/latest"
	@echo "   https://github.com/AuraFriday/mcp-link-server/releases/latest/download/AuraFriday-mcp-link-server-setup-v1.2.47-mac-arm.pkg"
	@echo "   https://github.com/AuraFriday/mcp-link-server/releases/latest/download/AuraFriday-mcp-link-server-setup-v1.2.47-mac-intel.pkg"
	@echo "   https://github.com/AuraFriday/mcp-link-server/releases/latest/download/AuraFriday-mcp-link-server-setup-v1.2.47-linux-x86_64.run"
	@echo "   https://github.com/AuraFriday/mcp-link-server/releases/latest/download/AuraFriday-mcp-link-server-setup-v1.2.47-windows-x86_64.exe"
	@echo




#first-run, use:	@echo "   GH_TOKEN=\"ghp_(token)\" gh release create latest --title \"Latest\" --notes \"Latest release v$(VERSION) built on $$(date +%F)\" "
