# CHANGELOG
All notable changes to this project are documented here.

## [2026-07-23] Refactor scraper to use requests and BeautifulSoup
Files changed:
- requirements.txt
- nixpacks.toml
- main.py
- test_scraper.py
Reason:
The camoufox headless browser caused heavy OS dependency issues and crashes on Railway. Replaced it with a lightweight HTTP requests-based scraper.
Related tests:
test_scraper.py
bn