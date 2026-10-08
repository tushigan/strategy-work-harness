"""Private bounded child for text previews; never used as a business authority."""
import difflib
import itertools
import json
from pathlib import Path
import sys

TEXT_TYPES={'.txt','.md','.json','.jsonl','.csv','.css','.html','.htm','.js','.mjs','.py','.toml','.yaml','.yml','.xml'}
MAX_BYTES=1024**2
MAX_LINES=10000

def preview(before,after):
    if Path(after).suffix.lower() not in TEXT_TYPES:return {'preview_omitted':'diff_type','diff_preview':[]}
    texts=[]
    for name in (before,after):
        if name is None:texts.append([]);continue
        path=Path(name)
        if path.stat().st_size>MAX_BYTES:return {'preview_omitted':'diff_size','diff_preview':[]}
        with path.open('rb') as f:raw=f.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES:return {'preview_omitted':'diff_size','diff_preview':[]}
        try:lines=raw.decode('utf-8').splitlines()
        except UnicodeError:return {'preview_omitted':'diff_type','diff_preview':[]}
        if len(lines)>MAX_LINES:return {'preview_omitted':'diff_lines','diff_preview':[]}
        texts.append(lines)
    return {'diff_preview':list(itertools.islice(difflib.unified_diff(*texts,n=1),40))}
if __name__=='__main__':
    print(json.dumps(preview(sys.argv[1] if sys.argv[1]!='-' else None,sys.argv[2]),ensure_ascii=False))
