# -*- coding: utf-8 -*-
"""逐屏截图回归对比：给定 before/after 两个目录，输出尺寸/哈希/像素差异 + 差异包围盒。

用途：任何视觉改动的"改前 vs 改后"证据（不只是"文件变了"，而是"变了多少、变在哪一块"）。

用法：python3 -X utf8 tools/_diff_shots.py before_dir after_dir [-o report.md]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

from PIL import Image, ImageChops


def sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('before')
    ap.add_argument('after')
    ap.add_argument('-o', '--out', default='')
    a = ap.parse_args()

    bd, ad = pathlib.Path(a.before), pathlib.Path(a.after)
    names = sorted({p.name for p in bd.glob('*.png')} | {p.name for p in ad.glob('*.png')})
    rows = []
    for n in names:
        b, c = bd / n, ad / n
        if not b.exists() or not c.exists():
            rows.append({'file': n, 'status': '缺一张', 'note': f'{b.exists()=} {c.exists()=}'})
            continue
        bi, ai = Image.open(b).convert('RGB'), Image.open(c).convert('RGB')
        same_size = bi.size == ai.size
        if same_size:
            diff = ImageChops.difference(bi, ai)
            bbox = diff.getbbox()
            w, h = bi.size
            nz = sum(1 for px in diff.getdata() if px != (0, 0, 0))
            pct = nz / (w * h) * 100
        else:
            bbox, pct = None, -1.0
        rows.append({
            'file': n, 'status': '相同' if sha(b) == sha(c) else '有差异',
            'before': f'{bi.size[0]}x{bi.size[1]} {len(b.read_bytes())}B {sha(b)[:10]}',
            'after': f'{ai.size[0]}x{ai.size[1]} {len(c.read_bytes())}B {sha(c)[:10]}',
            'diff_pct': round(pct, 2), 'bbox': bbox, 'same_size': same_size,
        })

    lines = ['| 截图 | 判定 | 改前 | 改后 | 差异像素 | 差异区域(bbox) |', '|---|---|---|---|---|---|']
    for r in rows:
        if 'before' not in r:
            lines.append(f"| {r['file']} | {r['status']} | - | - | - | {r.get('note','')} |")
            continue
        lines.append(f"| {r['file']} | {r['status']} | {r['before']} | {r['after']} | "
                     f"{r['diff_pct']}% | {r['bbox']} |")
    md = '\n'.join(lines)
    print(md)
    if a.out:
        pathlib.Path(a.out).write_text(md + '\n', encoding='utf-8')
        print(f'\n已写 {a.out}')
    (bd.parent / 'diff-raw.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
