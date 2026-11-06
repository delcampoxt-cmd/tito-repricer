#!/bin/bash
# Rewrites version.json after you copy a new repricer.py here. Usage: bash make-release.sh "notes"
cd "$(dirname "$0")"
/usr/bin/python3 - "$1" <<'PY'
import hashlib, json, re, sys
src=open("repricer.py",encoding="utf-8").read()
ver=re.search(r'^VERSION\s*=\s*"([^"]+)"',src,re.M).group(1)
d=open("repricer.py","rb").read()
json.dump({"version":ver,"notes":sys.argv[1] if len(sys.argv)>1 else "","files":{"repricer.py":{"sha256":hashlib.sha256(d).hexdigest(),"bytes":len(d)}}},open("version.json","w"),indent=1)
print("version.json written for v%s"%ver)
PY
