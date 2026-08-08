import os, zipfile, hashlib

SRC = r'C:\Users\TJM\WorkBuddy\2026-08-08-11-19-20\photo_sorter'
OUT = r'D:\AI本地大模型\work Buddy生成的软件（开源）\photo_sorter_v1.0.1.zip'
ROOT = 'photo_sorter'
SKIP_DIRS = {'.git', '__pycache__', 'venv', 'envs', '.photo_sorter_undo'}
SKIP_FILES = {'settings.json', 'make_release.py', 'CHECKSUMS.txt'}
EXTRA_SKIP_SUFFIX = ('.pyc', '.zip')

files = []
for dp, dns, fns in os.walk(SRC):
    dns[:] = [d for d in dns if d not in SKIP_DIRS]
    for fn in fns:
        fp = os.path.join(dp, fn)
        rel = os.path.relpath(fp, SRC)
        if rel in SKIP_FILES or rel.endswith(EXTRA_SKIP_SUFFIX):
            continue
        files.append((fp, rel))

checksum_lines = []
for fp, rel in files:
    h = hashlib.sha256(open(fp, 'rb').read()).hexdigest()
    checksum_lines.append(f'{h}  {rel}')
checksum_path = os.path.join(SRC, 'CHECKSUMS.txt')
with open(checksum_path, 'w', encoding='utf-8') as f:
    f.write('# SHA256 of files in this release (verify official integrity)\n')
    f.write('\n'.join(checksum_lines) + '\n')
files.append((checksum_path, 'CHECKSUMS.txt'))

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with zipfile.ZipFile(OUT, 'w', zipfile.ZIP_DEFLATED) as z:
    for fp, rel in files:
        z.write(fp, os.path.join(ROOT, rel))
import ctypes
ctypes.windll.kernel32.DeleteFileW(checksum_path)
print('ZIP ->', OUT, os.path.getsize(OUT) // 1024, 'KB, entries:', len(files))
