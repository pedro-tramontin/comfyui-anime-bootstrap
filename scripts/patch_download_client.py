#!/usr/bin/env python3
"""
Apply the download_client patch to a local copy of bootstrap.sh
(used both on the live pod via SSH and for the upstream PR).
"""
import sys, re

path = sys.argv[1] if len(sys.argv) > 1 else 'bootstrap.sh'

with open(path, 'r') as f:
    src = f.read()

original = src

# Patch 1: extend download_file signature with $5 download_client
old1 = '    local auth_type="$4"\n    local tmp="$dest.tmp"'
new1 = '    local auth_type="$4"\n    local download_client="${5:-aria2c}"\n    local tmp="$dest.tmp"'
assert old1 in src, "patch 1 anchor not found"
src = src.replace(old1, new1, 1)

# Patch 2: wrap the aria2c invocation in if [ "$download_client" = "wget" ]
# Find the aria2c start and the next "local rc=$?" line
start_pat = '    echo "  Downloading $name (${auth_type:-public}, aria2c)..."\n'
end_pat = '    local rc=$?\n    if [ $rc -ne 0 ] || [ ! -s "$tmp" ]; then'
i = src.find(start_pat)
assert i != -1, "patch 2 start anchor not found"
# Find the FIRST 'local rc=$?\n' after i
j = src.find('    local rc=$?\n', i)
assert j != -1, "patch 2 end anchor not found"
# End of the rc line
j_end = j + len('    local rc=$?\n')

# Block to wrap = from start_pat to j_end (inclusive of "local rc=$?\n")
aria_block = src[i:j_end]

# Indent the inner block by 4 spaces so it sits cleanly inside `else`
indented_aria = ''.join(
    ('    ' + line if line.strip() else line)
    for line in aria_block.splitlines(keepends=True)
)
# Make sure it ends with a newline
if not indented_aria.endswith('\n'):
    indented_aria += '\n'

wrap = (
    '    # Skip aria2c if the manifest entry opted into wget directly.\n'
    '    # Needed for civitai URLs whose 307 redirects to b2.civitai.com\n'
    '    # return 403 on the second hop with aria2c (the signed redirect\n'
    '    # URL gets rejected). wget follows the redirect cleanly because\n'
    '    # it makes a single GET that includes the original query string.\n'
    '    if [ "$download_client" = "wget" ]; then\n'
    '        echo "  Downloading $name (${auth_type:-public}, wget per manifest)..."\n'
    '        rc=1   # pretend aria2c failed; trigger the wget fallback below\n'
    '    else\n'
    + indented_aria +
    '    fi\n'
)
src = src[:i] + wrap + src[j_end:]

# Patch 3a: insert download_client jq line after auth_type line
old3a = '    auth_type=$(echo "$line" | jq -r \'.auth // "none"\')'
new3a = old3a + '\n    download_client=$(echo "$line" | jq -r \'.download_client // "aria2c"\')'
assert old3a in src, "patch 3a anchor not found"
src = src.replace(old3a, new3a, 1)

# Patch 3b: extend download_file call to pass download_client
old3b = '    download_file "$name" "$url" "$dest" "$auth_type" || true'
new3b = '    download_file "$name" "$url" "$dest" "$auth_type" "$download_client" || true'
assert old3b in src, "patch 3b anchor not found"
src = src.replace(old3b, new3b, 1)

# Patch 4: make the [fallback] message honest about which path triggered it
old4 = '        echo "  [fallback] aria2c failed (rc=$rc), trying wget..."'
new4 = (
    '        if [ "$download_client" = "wget" ]; then\n'
    '            echo "  [wget] downloading..."\n'
    '        else\n'
    '            echo "  [fallback] aria2c failed (rc=$rc), trying wget..."\n'
    '        fi'
)
assert old4 in src, "patch 4 anchor not found"
src = src.replace(old4, new4, 1)

with open(path, 'w') as f:
    f.write(src)

# Syntax check
import subprocess
r = subprocess.run(['bash', '-n', path], capture_output=True, text=True)
if r.returncode != 0:
    print('SYNTAX ERROR — reverting:', r.stderr)
    with open(path, 'w') as f:
        f.write(original)
    raise SystemExit(1)
print(f'patched {path} + bash -n passed')

# Show diff
import subprocess
r = subprocess.run(['diff', '-u', '/dev/stdin', path], input=original, capture_output=True, text=True)
print(r.stdout)
