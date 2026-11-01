"""只读核验收盘快照，输出排名前N的评分证据，不修改快照或重排股票。

python scripts/audit_quality_evidence.py --folder /path/to/close-data --limit 100
"""
import argparse
import csv
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.publish_close import validate_folder
from touzi.financial_coverage import quality_evidence


def audit(folder, limit=100):
    manifest, files = validate_folder(folder)
    entries = {}
    for shard in manifest['shards']:
        entries.update(json.loads(files[shard['path']])['entries'])
    # Snapshot index is already the published ranking; never sort on rounded scores.
    rows = []
    for rank, item in enumerate(manifest['index'][:limit], 1):
        entry = entries[item['code']]
        a = entry.get('financial_analysis') or {}
        q = a.get('quality') or {}
        evidence = quality_evidence(entry)
        if q.get('coverage') != evidence['coverage']:
            raise ValueError(f"{item['code']} coverage inconsistent")
        contributions = q.get('contributions') or {}
        for key, weight in evidence['effective_weights'].items():
            value = contributions.get(key)
            score = (q.get('sub_scores') or {}).get(key)
            if weight is None:
                if value is not None:
                    raise ValueError(f"{item['code']} missing/exempt item contributes")
            elif value is None or abs(value - weight * score) > 1e-8:
                raise ValueError(f"{item['code']} effective weight inconsistent")
        if any(v is not None for v in contributions.values()):
            actual = sum(v for v in contributions.values() if v is not None)
            if abs(actual - entry['quality']) > 1e-8:
                raise ValueError(f"{item['code']} contributions inconsistent")
        row = dict(rank=rank, code=item['code'], name=entry['name'], total=entry['total'],
                   quality=entry.get('quality'), action=entry['action'], industry=entry['industry'],
                   report_status=a.get('status'), source=a.get('source'),
                   indicator_source='sina via akshare (code provenance; per-item fetch time not recorded)', fetched_at=a.get('fetched_at'),
                   coverage=evidence['coverage'], pending=','.join(evidence['pending']), exempt=','.join(evidence['exempt']))
        for key, state in evidence['statuses'].items():
            row.update({key+'_status': state, key+'_score': (q.get('sub_scores') or {}).get(key),
                        key+'_weight': (q.get('weights') or {}).get(key), key+'_effective_weight': evidence['effective_weights'][key],
                        key+'_contribution': contributions.get(key)})
        rows.append(row)
    return rows


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', required=True)
    parser.add_argument('--limit', type=int, default=100)
    args = parser.parse_args()
    rows = audit(args.folder, args.limit)
    if rows:
        writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
