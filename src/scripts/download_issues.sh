#!/bin/bash

GITHUB_TOKEN_BIOCYPHER=${GITHUB_TOKEN_BIOCYPHER}
echo "Found token for GitHub API: ${GITHUB_TOKEN_BIOCYPHER:0:4}...${GITHUB_TOKEN_BIOCYPHER: -4}"

page=1
acc=$(mktemp)
echo "[]" > "$acc"

while true; do
  echo "Fetching page $page..."
  page_file=$(mktemp)
  curl -s -L \
    -H "Accept: application/vnd.github+json" \
    -H "Authorization: Bearer $GITHUB_TOKEN_BIOCYPHER" \
    -H "X-GitHub-Api-Version: 2026-03-10" \
    "https://api.github.com/repos/biocypher/biocypher/issues?per_page=100&page=$page&state=open" \
    -o "$page_file"

  count=$(python3 -c "
import json, sys
data = json.load(open('$page_file'))
if isinstance(data, dict):
    print('API error:', data.get('message', data), file=sys.stderr)
    sys.exit(1)
print(len(data))
")

  if [[ $? -ne 0 ]]; then
    rm "$page_file"
    exit 1
  fi

  if [[ "$count" -eq 0 ]]; then
    rm "$page_file"
    break
  fi

python3 -c "
import json
a = json.load(open('$acc'))
b = json.load(open('$page_file'))
json.dump(a + b, open('$acc', 'w'))
"
  rm "$page_file"

  if [[ "$count" -lt 100 ]]; then
    break
  fi

  page=$((page + 1))
done

cp "$acc" issues.json
rm "$acc"
echo "Done — $(python3 -c "import json; print(len(json.load(open('issues.json'))))") issues downloaded"