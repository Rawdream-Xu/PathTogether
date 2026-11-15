#!/usr/bin/env bash
# PathTogether 本地部署 —— 标注类别预建种子（P1-W1 验收 4 类）
# 复用平台同源创建路径 label_class_store.create_class（与 create_local_user.py
# 同一哲学：不绕平台不变量；owner actor 审计）。
# 幂等：名称已存在（DuplicateNameError）即跳过，可重复执行。
# 用法：bash 本地部署/seed_label_classes.sh
set -euo pipefail
PY=${PY:-$HOME/pt-venv/bin/python}
REPO=${REPO:-/mnt/f/切片三维重建软件平台构建/PathTogether}

"$PY" - "$REPO" <<'EOF'
import os
import sys

repo = sys.argv[1]
sys.path.insert(0, repo)
os.environ.setdefault(
    "DATABASE_URL", "postgresql://pt@127.0.0.1:5433/pathtogether")
os.environ.setdefault("STORAGE_BACKEND", "postgres")
os.environ.setdefault("UPLOAD_DIR", os.path.expanduser("~/pt-data/uploads"))
os.environ.setdefault("SHARE_DATA_DIR", os.path.expanduser("~/pt-data/share"))

# 隔离 env 对存储目录的要求：store 层只连 PG，但 import app 链不存在——
# label_class_store 只依赖 pg_store，无需其余目录。
import label_class_store as store

SEED = [
    ("黏膜", [163, 42, 55], 10),
    ("平滑肌", [226, 110, 115], 20),
    ("血管", [200, 30, 30], 30),
    ("神经", [90, 90, 200], 40),
]

created, skipped = [], []
for name, color, sort_order in SEED:
    try:
        row = store.create_class(name=name, color=color,
                                 sort_order=sort_order, created_by="owner-seed")
        created.append("%s(%s)" % (row["name"], ",".join(map(str, row["color"]))))
    except store.DuplicateNameError:
        skipped.append(name)

live = store.list_classes(enabled_only=True)
print("created: %s" % (", ".join(created) or "-"))
print("skipped(existing): %s" % (", ".join(skipped) or "-"))
print("live classes: %s" % ", ".join("%s%s" % (c["name"], c["color"]) for c in live))
EOF
